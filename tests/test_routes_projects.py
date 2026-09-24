import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")

from services import analysis_service  # noqa: E402
from tests.factories import completed_project, make_project, make_report  # noqa: E402

SUBMISSION = {
    "title": "Portfolio API",
    "github_repo_url": "https://github.com/ada/portfolio",
    "skills_claimed": ["Python", "FastAPI"],
}


@pytest.fixture
def scheduled(monkeypatch):
    """Capture what the route hands to the pipeline instead of running it.

    TestClient runs background tasks for real, and the real pipeline reaches
    GitHub and a model.
    """
    ran = []

    async def fake_run_pipeline(project_id, **kwargs):
        ran.append(project_id)
        return "completed"

    monkeypatch.setattr(analysis_service, "run_pipeline", fake_run_pipeline)
    return ran


# --- Submitting ---------------------------------------------------------------

def test_submitting_a_repository_accepts_it_and_starts_work(client, auth_headers, scheduled):
    response = client.post("/projects", json=SUBMISSION, headers=auth_headers)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    # 202, not 201: the row exists but the report the submitter wants does not.
    assert scheduled == [body["id"]]


def test_the_repository_owner_and_name_are_stored_at_submission(client, auth_headers, scheduled):
    body = client.post(
        "/projects",
        json={**SUBMISSION, "github_repo_url": "git@github.com:ada/portfolio.git"},
        headers=auth_headers,
    ).json()

    # The SSH form is accepted and canonicalised, so later API calls need no
    # re-parsing and the stored URL is one shape.
    assert body["repo_owner"] == "ada"
    assert body["repo_name"] == "portfolio"
    assert body["github_repo_url"] == "https://github.com/ada/portfolio"


def test_the_pipeline_is_scheduled_only_after_the_row_is_committed(
    client, auth_headers, db_session, monkeypatch
):
    from models.project import Project

    found = []

    async def fake_run_pipeline(project_id, **kwargs):
        # A separate session, as the real pipeline uses: it sees the row only
        # if the request committed before scheduling.
        found.append(db_session.get(Project, project_id) is not None)

    monkeypatch.setattr(analysis_service, "run_pipeline", fake_run_pipeline)
    client.post("/projects", json=SUBMISSION, headers=auth_headers)

    assert found == [True]


def test_a_url_that_is_not_a_github_repository_is_refused(client, auth_headers, scheduled):
    response = client.post(
        "/projects",
        json={**SUBMISSION, "github_repo_url": "https://gitlab.com/ada/portfolio"},
        headers=auth_headers,
    )

    assert response.status_code == 422
    assert scheduled == []


def test_server_owned_fields_cannot_be_set_by_the_client(client, auth_headers, scheduled):
    response = client.post(
        "/projects", json={**SUBMISSION, "status": "completed"}, headers=auth_headers
    )
    assert response.status_code == 422


def test_anonymous_callers_cannot_submit(client, scheduled):
    response = client.post("/projects", json=SUBMISSION)
    assert response.status_code in (401, 403)
    assert scheduled == []


# --- Listing and reading ------------------------------------------------------

def test_the_list_holds_only_the_callers_own_projects(
    client, auth_headers, db_session, user, other_user
):
    mine = make_project(db_session, user, title="Mine")
    make_project(db_session, other_user, title="Theirs")

    body = client.get("/projects", headers=auth_headers).json()

    assert [project["id"] for project in body] == [mine.id]


def test_another_users_project_is_reported_as_missing_not_forbidden(
    client, auth_headers, db_session, other_user
):
    theirs = make_project(db_session, other_user)

    response = client.get(f"/projects/{theirs.id}", headers=auth_headers)

    # 403 would confirm the id exists to someone with no right to it.
    assert response.status_code == 404


def test_a_project_still_running_has_no_report_yet(client, auth_headers, db_session, user):
    project = make_project(db_session, user, status="analyzing")

    body = client.get(f"/projects/{project.id}", headers=auth_headers).json()

    assert body["status"] == "analyzing"
    assert body["latest_report"] is None


def test_a_completed_project_carries_its_report(client, auth_headers, db_session, user):
    project, report = completed_project(db_session, user)

    body = client.get(f"/projects/{project.id}", headers=auth_headers).json()

    assert body["latest_report"]["id"] == report.id
    assert body["latest_report"]["skills"][0]["skill_name"] == "Python"
    assert body["latest_report"]["provenance"]["repo_commit_sha"] == "a" * 40


def test_the_newest_report_wins_after_a_re_analysis(client, auth_headers, db_session, user):
    project = make_project(db_session, user, status="completed")
    older = datetime.now(timezone.utc) - timedelta(days=2)
    make_report(db_session, project, created_at=older)
    newest = make_report(db_session, project)

    body = client.get(f"/projects/{project.id}", headers=auth_headers).json()

    # Re-analysis appends rather than replaces, so both exist and the project
    # counts as whatever it says now.
    assert body["latest_report"]["id"] == newest.id


def test_an_owner_sees_the_report_for_a_project_they_have_not_published(
    client, auth_headers, db_session, user
):
    project, report = completed_project(db_session, user, visibility="private")

    response = client.get(f"/projects/{project.id}/report", headers=auth_headers)

    # The public gate belongs to the public routes, not to the owner's view.
    assert response.status_code == 200
    assert response.json()["id"] == report.id


def test_the_report_endpoint_says_so_when_there_is_no_report(
    client, auth_headers, db_session, user
):
    project = make_project(db_session, user, status="fetching")

    response = client.get(f"/projects/{project.id}/report", headers=auth_headers)
    assert response.status_code == 404


def test_the_report_never_exposes_the_raw_model_response(
    client, auth_headers, db_session, user
):
    project, _ = completed_project(db_session, user)
    make_report  # keep the import honest when this test is read alone

    body = client.get(f"/projects/{project.id}/report", headers=auth_headers).json()

    assert "raw_response" not in body
    assert "token_usage" not in body


# --- Re-analysis --------------------------------------------------------------

def test_re_analysis_resets_the_project_and_schedules_another_run(
    client, auth_headers, db_session, user, scheduled
):
    project = make_project(db_session, user, status="failed",
                           error_message="The repository could not be reached.")

    response = client.post(f"/projects/{project.id}/reanalyze", headers=auth_headers)

    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert response.json()["error_message"] is None
    assert scheduled == [project.id]


@pytest.mark.parametrize("status", ["pending", "fetching", "analyzing", "attesting"])
def test_a_run_already_in_flight_is_not_scheduled_twice(
    client, auth_headers, db_session, user, scheduled, status
):
    project = make_project(db_session, user, status=status)

    response = client.post(f"/projects/{project.id}/reanalyze", headers=auth_headers)

    # Two pipelines over one row would race into the same columns.
    assert response.status_code == 409
    assert scheduled == []


def test_another_users_project_cannot_be_re_analysed(
    client, auth_headers, db_session, other_user, scheduled
):
    theirs = make_project(db_session, other_user, status="completed")

    response = client.post(f"/projects/{theirs.id}/reanalyze", headers=auth_headers)

    assert response.status_code == 404
    assert scheduled == []


# --- Deleting -----------------------------------------------------------------

def test_deleting_a_project_takes_its_snapshots_and_reports_with_it(
    client, auth_headers, db_session, user
):
    from models.analysis_report import AnalysisReport
    from models.repo_snapshot import RepoSnapshot

    project, report = completed_project(db_session, user)
    # Read before the delete: afterwards these are stale objects, and touching
    # an attribute would try to refresh a row that is gone.
    project_id, report_id = project.id, report.id

    assert client.delete(f"/projects/{project_id}", headers=auth_headers).status_code == 204

    db_session.expire_all()
    assert db_session.query(AnalysisReport).filter_by(id=report_id).count() == 0
    assert db_session.query(RepoSnapshot).filter_by(project_id=project_id).count() == 0


def test_another_users_project_cannot_be_deleted(
    client, auth_headers, db_session, other_user
):
    from models.project import Project

    theirs = make_project(db_session, other_user)
    project_id = theirs.id

    assert client.delete(f"/projects/{project_id}", headers=auth_headers).status_code == 404
    assert db_session.query(Project).filter_by(id=project_id).count() == 1
