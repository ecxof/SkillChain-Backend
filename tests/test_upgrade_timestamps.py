import hashlib
import os
import uuid
from datetime import datetime, timezone

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
from scripts import upgrade_timestamps as job  # noqa: E402
from services import timestamp_service  # noqa: E402
from services.attestation_service import AttestationLog  # noqa: E402
from services.timestamp_service import BitcoinAnchor, TimestampError  # noqa: E402

PENDING_PROOF = b"pending-proof"
UPGRADED_PROOF = b"upgraded-proof"


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def log(tmp_path):
    return AttestationLog(tmp_path / "attestations")


def add_report(session, *, status="pending", content=b"report", created_at=None):
    user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4()}@example.test")
    project = Project(id=str(uuid.uuid4()), user_id=user.id, title="Portfolio",
                      github_repo_url="https://github.com/octocat/portfolio",
                      repo_owner="octocat", repo_name="portfolio")
    snapshot = RepoSnapshot(id=str(uuid.uuid4()), project_id=project.id,
                            commit_sha="a" * 40, fetch_mode="public")
    report = AnalysisReport(
        id=str(uuid.uuid4()), repo_snapshot_id=snapshot.id, model_name="grok-3",
        prompt_version="v2", content_hash=hashlib.sha256(content).hexdigest(),
        timestamp_status=status, attestation_status="committed",
        created_at=created_at or datetime.now(timezone.utc),
    )
    session.add_all([user, project, snapshot, report])
    session.commit()
    return report


def stub_upgrade(monkeypatch, result):
    """Replace the calendar round-trip; its own behaviour is tested elsewhere."""
    calls = []

    def fake(proof, **kwargs):
        calls.append(proof)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(timestamp_service, "upgrade", fake)
    return calls


# --- Selecting work ----------------------------------------------------------

def test_only_pending_reports_with_a_hash_are_attempted(session, log, monkeypatch):
    pending = add_report(session, status="pending")
    add_report(session, status="anchored", content=b"other")
    add_report(session, status="none", content=b"third")
    add_report(session, status="failed", content=b"fourth")

    assert [r.id for r in job.pending_reports(session, 10)] == [pending.id]


def test_the_batch_size_is_respected(session, log):
    for index in range(5):
        add_report(session, content=f"report-{index}".encode())
    assert len(job.pending_reports(session, 3)) == 3


# --- Upgrading ---------------------------------------------------------------

def test_a_confirmed_proof_anchors_the_report(session, log, monkeypatch):
    report = add_report(session)
    log.append_proof(report.content_hash, PENDING_PROOF)
    stub_upgrade(monkeypatch, (UPGRADED_PROOF, BitcoinAnchor(block_height=870_123)))

    assert job.upgrade_report(session, log, report) == "anchored"

    session.refresh(report)
    assert report.timestamp_status == "anchored"
    assert report.bitcoin_block_height == 870_123
    assert report.anchored_at is not None
    # The completed proof replaces the pending one in the log.
    assert log.read_proof(report.content_hash) == UPGRADED_PROOF


def test_a_proof_bitcoin_has_not_confirmed_is_left_alone(session, log, monkeypatch):
    report = add_report(session)
    log.append_proof(report.content_hash, PENDING_PROOF)
    stub_upgrade(monkeypatch, (PENDING_PROOF, None))

    assert job.upgrade_report(session, log, report) == "waiting"

    session.refresh(report)
    # Not a failure: confirmation takes hours, so the next run tries again.
    assert report.timestamp_status == "pending"
    assert report.bitcoin_block_height is None


def test_a_report_with_no_stored_proof_stops_being_retried(session, log, monkeypatch):
    report = add_report(session)
    calls = stub_upgrade(monkeypatch, (UPGRADED_PROOF, None))

    assert job.upgrade_report(session, log, report) == "missing"

    session.refresh(report)
    # Pending in the database but absent from the log: the stamping run died
    # between the two, and no amount of retrying will conjure a proof.
    assert report.timestamp_status == "failed"
    assert calls == []


def test_an_unreadable_proof_is_recorded_as_failed(session, log, monkeypatch):
    report = add_report(session)
    log.append_proof(report.content_hash, b"not an ots file")
    stub_upgrade(monkeypatch, TimestampError("the stored .ots proof is not readable"))

    assert job.upgrade_report(session, log, report) == "failed"
    session.refresh(report)
    assert report.timestamp_status == "failed"


def test_a_log_that_cannot_record_the_upgrade_stays_pending(session, tmp_path, monkeypatch):
    broken = AttestationLog(tmp_path / "attestations", signing_key=tmp_path / "absent")
    report = add_report(session)
    (broken.repo_path / "reports" / report.content_hash[:2]).mkdir(parents=True)
    (broken.repo_path / "reports" / report.content_hash[:2]
     / f"{report.content_hash}.ots").write_bytes(PENDING_PROOF)
    stub_upgrade(monkeypatch, (UPGRADED_PROOF, BitcoinAnchor(block_height=870_123)))

    assert job.upgrade_report(session, broken, report) == "failed"

    session.refresh(report)
    # The anchor is real, but claiming it without recording the proof would
    # leave a report nobody can verify. Retry next run instead.
    assert report.timestamp_status == "pending"


# --- The batch ---------------------------------------------------------------

def test_upgrade_pending_reports_what_happened_to_each(session, log, monkeypatch):
    # Explicit timestamps: the job takes the oldest first, so these fix the
    # order the stubbed outcomes below are handed out in.
    anchored = add_report(session, content=b"one", created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    waiting = add_report(session, content=b"two", created_at=datetime(2026, 9, 2, tzinfo=timezone.utc))
    add_report(session, content=b"three",  # stamped, but nothing was ever stored
               created_at=datetime(2026, 9, 3, tzinfo=timezone.utc))
    log.append_proof(anchored.content_hash, PENDING_PROOF)
    log.append_proof(waiting.content_hash, PENDING_PROOF)

    outcomes = iter([
        (UPGRADED_PROOF, BitcoinAnchor(block_height=870_123)),  # Bitcoin confirmed
        (PENDING_PROOF, None),                                  # still waiting
    ])
    monkeypatch.setattr(timestamp_service, "upgrade", lambda proof, **kw: next(outcomes))

    counts = job.upgrade_pending(session, log)

    assert counts == {"anchored": 1, "waiting": 1, "missing": 1, "failed": 0}


def test_mirrors_are_only_pushed_when_something_was_anchored(session, log, monkeypatch):
    report = add_report(session)
    log.append_proof(report.content_hash, PENDING_PROOF)
    stub_upgrade(monkeypatch, (PENDING_PROOF, None))
    pushes = []
    monkeypatch.setattr(log, "push", lambda: pushes.append(True) or [])

    job.upgrade_pending(session, log)
    assert pushes == []

    stub_upgrade(monkeypatch, (UPGRADED_PROOF, BitcoinAnchor(block_height=1)))
    job.upgrade_pending(session, log)
    assert pushes == [True]


# --- The command -------------------------------------------------------------

def test_the_command_does_nothing_when_timestamping_is_off(monkeypatch, capsys):
    monkeypatch.setattr(timestamp_service, "is_enabled", lambda: False)
    assert job.main([]) == 0


def test_the_command_fails_loudly_when_the_log_is_not_configured(monkeypatch):
    from services import attestation_service

    monkeypatch.setattr(timestamp_service, "is_enabled", lambda: True)

    def refuse():
        raise attestation_service.AttestationError("ATTESTATION_REPO_PATH is not set")

    monkeypatch.setattr(attestation_service, "from_env", refuse)
    # A misconfigured scheduled job must not exit 0 and look healthy.
    assert job.main([]) == 1
