import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite://")

from services import attestation_service  # noqa: E402
from tests.factories import completed_project, make_project, make_report  # noqa: E402


def publish(db_session, user, slug="ada"):
    user.profile_slug = slug
    user.profile_is_public = True
    db_session.commit()
    return user


# --- Profiles -----------------------------------------------------------------

def test_a_published_profile_is_served_without_a_token(client, db_session, user):
    publish(db_session, user)
    completed_project(db_session, user, visibility="public")

    body = client.get("/public/profiles/ada").json()

    assert body["display_name"] == "Ada Lovelace"
    assert [skill["skill_name"] for skill in body["skills"]] == ["Python"]
    assert body["skills"][0]["highest_level"] == "advanced"


def test_an_unpublished_profile_is_indistinguishable_from_no_profile(
    client, db_session, user
):
    user.profile_slug = "ada"
    user.profile_is_public = False
    db_session.commit()

    assert client.get("/public/profiles/ada").status_code == 404
    # Same answer for a slug nobody ever claimed.
    assert client.get("/public/profiles/nobody").status_code == 404


def test_a_private_project_never_reaches_a_public_profile(client, db_session, user):
    publish(db_session, user)
    completed_project(db_session, user, visibility="private")

    body = client.get("/public/profiles/ada").json()

    # Both gates must be open: the profile is published, but the project is not.
    assert body["skills"] == []


def test_an_unverified_skill_is_not_claimed_on_the_profile(client, db_session, user):
    publish(db_session, user)
    project = make_project(db_session, user, status="completed", visibility="public")
    make_report(db_session, project, verified=False)

    body = client.get("/public/profiles/ada").json()

    assert body["skills"] == []


def test_the_profile_links_to_the_reports_behind_each_skill(client, db_session, user):
    publish(db_session, user)
    _, report = completed_project(db_session, user, visibility="public")

    body = client.get("/public/profiles/ada").json()

    # The evidence has to be reachable, or the claim is just an assertion.
    assert body["skills"][0]["report_ids"] == [report.id]
    assert client.get(f"/public/reports/{report.id}").status_code == 200


# --- Reports ------------------------------------------------------------------

def test_a_report_on_a_public_project_is_readable_by_anyone(client, db_session, user):
    _, report = completed_project(db_session, user, visibility="public")

    body = client.get(f"/public/reports/{report.id}").json()

    assert body["id"] == report.id
    assert body["overall_trust_score"] == 82
    assert body["skills"][0]["evidence"][0]["file"] == "services/analysis_service.py"


def test_a_report_on_a_private_project_is_reported_as_missing(client, db_session, user):
    _, report = completed_project(db_session, user, visibility="private")

    # Not 403: that would confirm the report exists.
    assert client.get(f"/public/reports/{report.id}").status_code == 404


def test_a_report_on_a_project_still_running_is_not_served(client, db_session, user):
    project = make_project(db_session, user, status="analyzing", visibility="public")
    report = make_report(db_session, project)

    assert client.get(f"/public/reports/{report.id}").status_code == 404


def test_a_superseded_report_stays_readable(client, db_session, user):
    project = make_project(db_session, user, status="completed", visibility="public")
    older = make_report(db_session, project,
                        created_at=datetime.now(timezone.utc) - timedelta(days=2))
    make_report(db_session, project)

    # Re-analysis appends rather than replaces. If only the newest were served,
    # a developer could quietly retire a verdict they had already shared.
    assert client.get(f"/public/reports/{older.id}").status_code == 200


def test_an_unknown_report_id_is_a_plain_404(client):
    assert client.get("/public/reports/not-a-real-id").status_code == 404


def test_the_public_report_withholds_the_raw_model_response(client, db_session, user):
    _, report = completed_project(db_session, user, visibility="public")

    body = client.get(f"/public/reports/{report.id}").json()

    assert "raw_response" not in body
    assert "token_usage" not in body


# --- The signing key ----------------------------------------------------------

def test_the_signing_key_is_published_where_a_verifier_would_look(client, monkeypatch):
    class Log:
        def allowed_signers(self):
            return "attestation@skillchain ssh-ed25519 AAAAC3Nz...\n"

    monkeypatch.setattr(attestation_service, "from_env", Log)

    response = client.get("/.well-known/skillchain-signing-key")

    assert response.status_code == 200
    assert response.text.startswith("attestation@skillchain ssh-ed25519")
    assert response.headers["content-type"].startswith("text/plain")


def test_a_deployment_with_no_attestation_says_so_rather_than_failing(client, monkeypatch):
    def unconfigured():
        raise attestation_service.AttestationError("ATTESTATION_REPO_PATH is not set")

    monkeypatch.setattr(attestation_service, "from_env", unconfigured)

    # 404, not 500: nothing is broken and the caller did nothing wrong.
    assert client.get("/.well-known/skillchain-signing-key").status_code == 404
