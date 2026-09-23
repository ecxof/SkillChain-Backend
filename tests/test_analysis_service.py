import asyncio
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import models  # noqa: E402,F401
from db.database import Base  # noqa: E402
from models.analysis_report import AnalysisReport  # noqa: E402
from models.project import Project  # noqa: E402
from models.repo_snapshot import RepoSnapshot  # noqa: E402
from models.user import User  # noqa: E402
from services import analysis_service, attestation_service, timestamp_service  # noqa: E402
from services.ai_service import AnalysisError, AnalysisResult  # noqa: E402
from services.attestation_service import AttestationError, AttestationLog  # noqa: E402
from services.github_service import RateLimited, RepositoryNotFound  # noqa: E402
from services.timestamp_service import TimestampError  # noqa: E402

HEAD = "a3f9c1b" + "0" * 33


@pytest.fixture
def session_factory():
    """A sessionmaker the pipeline can open and close its own sessions from."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, autoflush=False)
    finally:
        engine.dispose()


@pytest.fixture
def project(session_factory):
    session = session_factory()
    user = User(id=str(uuid.uuid4()), email="octocat@example.test",
                github_username="octocat", github_access_token="ghp_token",
                display_name="The Octocat")
    project = Project(
        id=str(uuid.uuid4()), user_id=user.id, title="Portfolio",
        github_repo_url="https://github.com/octocat/portfolio",
        repo_owner="octocat", repo_name="portfolio",
        skills_claimed=["React", "CSS"], role_in_project="Sole developer",
        visibility="public",
    )
    session.add_all([user, project])
    session.commit()
    project_id = project.id
    session.close()
    return project_id


def repo_data(**overrides):
    fields = {
        "commit_sha": HEAD, "default_branch": "main",
        "languages": {"JavaScript": 900}, "topics": ["react"], "stars": 3, "forks": 1,
        "commit_count": 341, "user_commit_count": 62, "readme_content": "# Portfolio",
        "file_tree": {"truncated": False, "entry_count": 1,
                      "entries": [{"path": "src/App.jsx", "size": 300}]},
        "sampled_files": {"src/App.jsx": "export default function App() {\n  return null;\n}\n"},
        "commit_history": [], "contributor_stats": None, "fetch_mode": "public",
        "fetched_at": datetime.now(timezone.utc),
    }
    fields.update(overrides)
    return SimpleNamespace(snapshot_fields=lambda: dict(fields))


def analysis_result(**overrides):
    defaults = {
        "overall_trust_score": 84, "summary": "A React portfolio.",
        "skills": [{
            "skill_name": "React", "claimed": True, "verified": True,
            "level": "advanced", "confidence": 88, "reason": "Hooks throughout.",
            "evidence": [{"type": "file", "file": "src/App.jsx",
                          "start_line": 1, "end_line": 3, "detail": "component"}],
        }],
        "model_name": "grok-3", "prompt_version": "v2", "raw_response": "{}",
        "duration_ms": 1200, "token_usage": {"total": 900}, "attempts": 1,
        "rejected_citations": [],
    }
    defaults.update(overrides)
    return AnalysisResult(**defaults)


@pytest.fixture
def pipeline(monkeypatch):
    """Runs the pipeline with GitHub and the model stubbed out.

    Both are exercised for real elsewhere; here the point is the orchestration
    around them — statuses, persistence and what survives a failure.
    """
    state = SimpleNamespace(
        data=repo_data(), result=analysis_result(),
        fetch_error=None, analyse_error=None, statuses=[],
    )

    async def fake_fetch(owner, name, **kwargs):
        state.fetch_call = (owner, name, kwargs)
        if state.fetch_error:
            raise state.fetch_error
        return state.data

    async def fake_analyse(snapshot, skills_claimed, role_in_project=None, provider=None):
        state.analyse_call = (snapshot, skills_claimed, role_in_project)
        if state.analyse_error:
            raise state.analyse_error
        return state.result

    real_advance = analysis_service._advance

    def recording_advance(session, project, status):
        state.statuses.append(status)
        real_advance(session, project, status)

    monkeypatch.setattr(analysis_service, "fetch_repository", fake_fetch)
    monkeypatch.setattr(analysis_service, "analyze_snapshot", fake_analyse)
    monkeypatch.setattr(analysis_service, "_advance", recording_advance)
    return state


def run(project_id, session_factory, **kwargs):
    return asyncio.run(
        analysis_service.run_pipeline(project_id, session_factory=session_factory, **kwargs)
    )


# --- The happy path ----------------------------------------------------------

def test_a_successful_run_walks_the_statuses_and_completes(project, session_factory, pipeline):
    assert run(project, session_factory) == "completed"
    assert pipeline.statuses == ["fetching", "analyzing", "attesting", "completed"]

    session = session_factory()
    stored = session.get(Project, project)
    assert stored.status == "completed"
    assert stored.error_message is None


def test_a_successful_run_persists_the_snapshot_report_and_skills(
        project, session_factory, pipeline):
    run(project, session_factory)

    session = session_factory()
    snapshot = session.query(RepoSnapshot).one()
    report = session.query(AnalysisReport).one()

    assert snapshot.project_id == project
    assert snapshot.commit_sha == HEAD
    # Sampled contents are kept so evidence line ranges still resolve later.
    assert snapshot.sampled_files["src/App.jsx"].startswith("export default")
    assert report.repo_snapshot_id == snapshot.id
    assert (report.model_name, report.prompt_version) == ("grok-3", "v2")
    assert report.overall_trust_score == 84
    assert report.authorship_signals is not None
    assert [skill.skill_name for skill in report.skills] == ["React"]
    assert report.skills[0].evidence[0]["file"] == "src/App.jsx"


def test_the_submitters_token_and_username_reach_github(project, session_factory, pipeline):
    run(project, session_factory)
    owner, name, kwargs = pipeline.fetch_call
    assert (owner, name) == ("octocat", "portfolio")
    assert kwargs["token"] == "ghp_token"
    # Needed to work out the submitter's share of the commits.
    assert kwargs["submitter_login"] == "octocat"


def test_the_claimed_skills_and_role_reach_the_model(project, session_factory, pipeline):
    run(project, session_factory)
    _, skills_claimed, role = pipeline.analyse_call
    assert skills_claimed == ["React", "CSS"]
    assert role == "Sole developer"


def test_a_report_is_addressed_even_when_publishing_is_switched_off(
        project, session_factory, pipeline):
    run(project, session_factory)

    report = session_factory().query(AnalysisReport).one()
    # The hash is the report's identity, not a by-product of the log.
    assert len(report.content_hash) == 64
    assert report.attestation_status == "pending"
    assert report.attestation_commit_sha is None
    assert report.timestamp_status == "none"


def test_reanalysis_adds_a_second_snapshot_and_report(project, session_factory, pipeline):
    run(project, session_factory)
    pipeline.data = repo_data(commit_sha="b" * 40)
    pipeline.result = analysis_result(overall_trust_score=91)

    assert run(project, session_factory) == "completed"

    session = session_factory()
    # The history of what was said about this repository stays intact.
    assert session.query(RepoSnapshot).count() == 2
    assert sorted(r.overall_trust_score for r in session.query(AnalysisReport)) == [84, 91]


# --- Failures ----------------------------------------------------------------

def test_a_missing_repository_fails_with_the_message_github_gave(
        project, session_factory, pipeline):
    pipeline.fetch_error = RepositoryNotFound("octocat/portfolio", may_be_private=True)

    assert run(project, session_factory) == "failed"

    stored = session_factory().get(Project, project)
    assert stored.status == "failed"
    # The submitter reads this, so it has to say something actionable.
    assert "Link your GitHub account" in stored.error_message


def test_a_rate_limit_fails_the_run_without_a_snapshot(project, session_factory, pipeline):
    pipeline.fetch_error = RateLimited(reset_at=None, authenticated=False)

    assert run(project, session_factory) == "failed"

    session = session_factory()
    assert session.query(RepoSnapshot).count() == 0
    assert "rate limit" in session.get(Project, project).error_message


def test_a_model_failure_fails_the_run_but_keeps_the_snapshot(
        project, session_factory, pipeline):
    pipeline.analyse_error = AnalysisError("the provider returned nothing usable")

    assert run(project, session_factory) == "failed"

    session = session_factory()
    # The fetch already happened and cost rate limit; keep what it produced.
    assert session.query(RepoSnapshot).count() == 1
    assert session.query(AnalysisReport).count() == 0
    assert session.get(Project, project).status == "failed"


def test_an_unexpected_crash_does_not_leak_its_details(project, session_factory, pipeline):
    pipeline.analyse_error = ZeroDivisionError("division by zero")

    assert run(project, session_factory) == "failed"

    message = session_factory().get(Project, project).error_message
    assert message == analysis_service.UNEXPECTED_ERROR
    assert "division" not in message


def test_a_long_failure_message_is_trimmed(project, session_factory, pipeline):
    pipeline.fetch_error = RepositoryNotFound("x" * 2000, may_be_private=False)
    run(project, session_factory)
    message = session_factory().get(Project, project).error_message
    assert len(message) <= analysis_service.MAX_ERROR_CHARS


def test_a_project_deleted_while_queued_is_not_an_error(session_factory, pipeline):
    assert run(str(uuid.uuid4()), session_factory) == "missing"


# --- Publishing --------------------------------------------------------------

@pytest.fixture
def enabled_log(monkeypatch, tmp_path):
    monkeypatch.setattr(attestation_service, "is_enabled", lambda: True)
    return AttestationLog(tmp_path / "attestations")


def test_an_enabled_log_records_the_commit_on_the_report(
        project, session_factory, pipeline, enabled_log):
    run(project, session_factory, log=enabled_log)

    report = session_factory().query(AnalysisReport).one()
    assert report.attestation_status == "committed"
    assert report.attestation_commit_sha
    assert report.attested_at is not None
    # What was committed is what the report says it is.
    committed = (enabled_log.repo_path
                 / attestation_service.report_path(report.content_hash))
    assert committed.exists()


def test_a_mirrored_log_is_recorded_as_mirrored(
        project, session_factory, pipeline, monkeypatch, tmp_path):
    import subprocess
    mirror = tmp_path / "mirror.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(mirror)], check=True)
    monkeypatch.setattr(attestation_service, "is_enabled", lambda: True)
    log = AttestationLog(tmp_path / "attestations", remotes=[str(mirror)])

    run(project, session_factory, log=log)

    report = session_factory().query(AnalysisReport).one()
    # Mirrored means a host other than this machine now holds it.
    assert report.attestation_status == "mirrored"


def test_an_attestation_failure_does_not_lose_the_analysis(
        project, session_factory, pipeline, monkeypatch, tmp_path):
    monkeypatch.setattr(attestation_service, "is_enabled", lambda: True)
    broken = AttestationLog(tmp_path / "attestations", signing_key=tmp_path / "absent")

    assert run(project, session_factory, log=broken) == "completed"

    report = session_factory().query(AnalysisReport).one()
    # The analysis was paid for and is still worth serving.
    assert report.attestation_status == "failed"
    assert report.overall_trust_score == 84
    assert session_factory().get(Project, project).status == "completed"


def test_timestamping_stores_a_pending_proof_beside_the_report(
        project, session_factory, pipeline, monkeypatch, enabled_log):
    monkeypatch.setattr(timestamp_service, "is_enabled", lambda: True)
    monkeypatch.setattr(timestamp_service, "stamp", lambda digest: b"proof-for-" + digest)

    run(project, session_factory, log=enabled_log)

    report = session_factory().query(AnalysisReport).one()
    # Pending, not anchored: Bitcoin confirms hours later.
    assert report.timestamp_status == "pending"
    proof = enabled_log.repo_path / attestation_service.proof_path(report.content_hash)
    assert proof.read_bytes() == b"proof-for-" + bytes.fromhex(report.content_hash)


def test_unreachable_calendars_do_not_lose_the_analysis(
        project, session_factory, pipeline, monkeypatch, enabled_log):
    monkeypatch.setattr(timestamp_service, "is_enabled", lambda: True)

    def unreachable(digest):
        raise TimestampError("no calendar accepted the digest")

    monkeypatch.setattr(timestamp_service, "stamp", unreachable)

    assert run(project, session_factory, log=enabled_log) == "completed"

    report = session_factory().query(AnalysisReport).one()
    assert report.timestamp_status == "failed"
    # The git signature is unaffected: it never waits on Bitcoin.
    assert report.attestation_status == "committed"


def test_timestamping_is_skipped_when_the_log_is_off(
        project, session_factory, pipeline, monkeypatch):
    monkeypatch.setattr(timestamp_service, "is_enabled", lambda: True)
    called = []
    monkeypatch.setattr(timestamp_service, "stamp", lambda digest: called.append(digest))

    run(project, session_factory)

    # There is nowhere to put a proof without a log, so nothing is stamped.
    assert called == []
    assert session_factory().query(AnalysisReport).one().timestamp_status == "none"


def test_a_misconfigured_log_is_recorded_rather_than_raising(
        project, session_factory, pipeline, monkeypatch):
    monkeypatch.setattr(attestation_service, "is_enabled", lambda: True)

    def refuse():
        raise AttestationError("ATTESTATION_SIGNING_KEY_PATH is not set")

    monkeypatch.setattr(attestation_service, "from_env", refuse)

    assert run(project, session_factory) == "completed"
    assert session_factory().query(AnalysisReport).one().attestation_status == "failed"
