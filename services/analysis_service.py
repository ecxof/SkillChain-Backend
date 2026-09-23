"""Running one project's analysis from submission to a published report.

:func:`run_pipeline` is the whole flow: fetch the repository, store the
snapshot, derive authorship signals, ask the model, persist the verdicts, then
address and publish the report. It is scheduled as a background task, so it
owns its own database session and never raises into the request that started
it — every failure lands in ``Project.status`` and ``Project.error_message``,
which is what the client polls.

The layers fail independently and on purpose. A repository that cannot be
fetched or a model that never returns anything usable fails the run. A failure
to sign, mirror or timestamp does not: the analysis has already been paid for
and is still worth serving, so it is recorded on the report and can be retried
later. That is why ``attestation_status`` and ``timestamp_status`` live on the
report rather than collapsing into the project's status.
"""

import asyncio
import logging

from db.database import SessionLocal
from models.analysis_report import AnalysisReport
from models.project import Project
from models.repo_snapshot import RepoSnapshot
from models.skill_assessment import SkillAssessment
from services import attestation_service, timestamp_service
from services.ai_service import AnalysisError, analyze_snapshot
from services.authorship_service import derive_authorship_signals
from services.github_service import GitHubError, fetch_repository

logger = logging.getLogger(__name__)

# Shown to the submitter, so it is trimmed rather than carrying a whole trace.
MAX_ERROR_CHARS = 500
UNEXPECTED_ERROR = "The analysis failed unexpectedly. Please try again."


async def run_pipeline(project_id: str, *, session_factory=SessionLocal,
                       provider=None, log=None) -> str:
    """Analyse one project and return the status it finished in.

    ``provider`` and ``log`` are injected by tests; in production both are
    built from the environment by the services that need them.
    """
    session = session_factory()
    project = None
    try:
        project = session.get(Project, project_id)
        if project is None:
            # The submitter deleted it while it sat in the queue.
            logger.warning("analysis asked for project %s, which no longer exists", project_id)
            return "missing"

        snapshot = await _fetch(session, project)
        report = await _analyse(session, project, snapshot, provider)
        await _publish(session, project, report, log=log)

        _advance(session, project, "completed")
        return "completed"

    except (GitHubError, AnalysisError) as exc:
        # Expected, explainable failures: the submitter reads this message.
        _fail(session, project, str(exc))
        return "failed"
    except Exception:
        logger.exception("the analysis pipeline crashed for project %s", project_id)
        _fail(session, project, UNEXPECTED_ERROR)
        return "failed"
    finally:
        session.close()


async def _fetch(session, project) -> RepoSnapshot:
    """Pull the repository and store exactly what was seen."""
    _advance(session, project, "fetching")
    user = project.user
    data = await fetch_repository(
        project.repo_owner, project.repo_name,
        token=getattr(user, "github_access_token", None),
        submitter_login=getattr(user, "github_username", None),
    )
    snapshot = RepoSnapshot(project_id=project.id, **data.snapshot_fields())
    session.add(snapshot)
    session.commit()
    session.refresh(snapshot)
    return snapshot


async def _analyse(session, project, snapshot, provider) -> AnalysisReport:
    """Derive the deterministic signals, then ask the model for its verdicts."""
    _advance(session, project, "analyzing")
    signals = derive_authorship_signals(
        snapshot, github_username=getattr(project.user, "github_username", None)
    )
    result = await analyze_snapshot(
        snapshot, project.skills_claimed or [], project.role_in_project, provider
    )

    report = AnalysisReport(
        repo_snapshot_id=snapshot.id, authorship_signals=signals,
        **result.report_fields(),
    )
    session.add(report)
    session.flush()  # assigns report.id, which the skill rows need
    for skill in result.skills:
        session.add(SkillAssessment(report_id=report.id, **skill))
    session.commit()
    # created_at is a server default and is part of the published document.
    session.refresh(report)
    return report


async def _publish(session, project, report, *, log=None) -> None:
    """Address the report, then publish it to the log and to Bitcoin.

    The content hash is computed whether or not publishing is switched on: it
    is the report's identity, not a by-product of the log, and having it means
    a later attestation run recognises a report it has already published.
    """
    _advance(session, project, "attesting")

    canonical = attestation_service.canonical_json(attestation_service.build_document(report))
    report.content_hash = attestation_service.content_hash(canonical)
    session.commit()

    if not attestation_service.is_enabled():
        # 'pending' is honest here: addressed, not yet in the log.
        return

    try:
        attestation_log = log or attestation_service.from_env()
        record = await asyncio.to_thread(
            attestation_service.attest, report, log=attestation_log
        )
    except attestation_service.AttestationError as exc:
        logger.warning("could not attest report %s: %s", report.id, exc)
        report.attestation_status = "failed"
        session.commit()
        return

    report.attestation_commit_sha = record.commit_sha
    # Mirrored means at least one host other than this machine now holds it.
    report.attestation_status = "mirrored" if record.mirrors else "committed"
    report.attested_at = record.attested_at
    session.commit()

    if not timestamp_service.is_enabled():
        return

    try:
        proof = await asyncio.to_thread(
            timestamp_service.stamp, bytes.fromhex(report.content_hash)
        )
        await asyncio.to_thread(
            attestation_log.append_proof, report.content_hash, proof
        )
    except (timestamp_service.TimestampError, attestation_service.AttestationError) as exc:
        logger.warning("could not timestamp report %s: %s", report.id, exc)
        report.timestamp_status = "failed"
    else:
        # Bitcoin confirms hours later; upgrade_timestamps.py finishes the job.
        report.timestamp_status = "pending"
    session.commit()


def _advance(session, project, status: str) -> None:
    project.status = status
    project.error_message = None
    session.commit()


def _fail(session, project, message: str) -> None:
    """Record why the run stopped, without letting the failure escape.

    Rolls back first: the session may hold a half-finished transaction from
    whatever raised, and the status update has to survive independently of it.
    """
    if project is None:
        return
    try:
        session.rollback()
        project.status = "failed"
        project.error_message = message[:MAX_ERROR_CHARS]
        session.commit()
    except Exception:
        logger.exception("could not record the failure of project %s", project.id)
