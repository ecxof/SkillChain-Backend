"""The unauthenticated surface: what a recruiter can read without an account.

Nothing here requires a token, so every endpoint has to decide for itself
what it is allowed to show. Two gates do that work. A profile is served only
when its owner published it; a report is served only when the project behind
it is public and finished.

Anything that fails a gate is reported as missing, never as forbidden. A
recruiter following a stale link and a stranger guessing at ids get the same
answer, so the API never confirms that a private project exists.
"""

import base64
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from db.database import get_db
from models.analysis_report import AnalysisReport
from models.project import Project
from models.repo_snapshot import RepoSnapshot
from schemas.profiles import PublicProfileOut
from schemas.reports import AttestationOut, ReportOut
from services import attestation_service
from services.attestation_service import AttestationRecord
from services.profile_service import get_public_profile

logger = logging.getLogger(__name__)

router = APIRouter()

NOT_FOUND = "No published report at that address"


def _published_report(db: Session, report_id: str) -> AnalysisReport:
    """A report whose project is public and finished, or 404.

    Older reports of a public project stay readable. Re-analysis appends
    rather than replaces, and serving only the newest would let a developer
    quietly retire a verdict they had already shared.
    """
    report = (
        db.query(AnalysisReport)
        .join(RepoSnapshot, AnalysisReport.repo_snapshot_id == RepoSnapshot.id)
        .join(Project, RepoSnapshot.project_id == Project.id)
        .filter(
            AnalysisReport.id == report_id,
            Project.visibility == "public",
            Project.status == "completed",
        )
        .first()
    )
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return report


@router.get("/profiles/{slug}", response_model=PublicProfileOut)
def public_profile(slug: str, db: Session = Depends(get_db)):
    """A developer's verified skills, aggregated across their public projects.

    The profile is the thing shared; the reports behind each skill are the
    evidence a reader drills into.
    """
    profile = get_public_profile(db, slug)
    if profile is None:
        # Covers both 'no such slug' and 'not published', which a visitor has
        # no business being able to tell apart.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No public profile at that address")
    return profile


@router.get("/reports/{report_id}", response_model=ReportOut)
def public_report(report_id: str, db: Session = Depends(get_db)):
    """One report in full: verdicts, span evidence, authorship, provenance.

    This is what a claim on a profile resolves to, and it is deliberately
    readable without an account: evidence nobody can open convinces nobody.
    """
    return ReportOut.from_report(_published_report(db, report_id))


def _log():
    """The configured attestation log, or None on a deployment without one.

    Attestation is optional, so an unconfigured deployment still serves the
    report and simply has no proof to offer alongside it.
    """
    try:
        return attestation_service.from_env()
    except attestation_service.AttestationError as exc:
        logger.warning("attestation asked for but not configured: %s", exc)
        return None


def _canonical_bytes(report: AnalysisReport) -> bytes | None:
    """The exact bytes that were hashed and committed, re-derived.

    Recomputing rather than storing keeps one definition of the document, and
    it doubles as an integrity check: if these bytes no longer hash to the
    stored content_hash, the row has drifted from what was published, and
    serving them under the published hash would be a false proof.
    """
    if not report.content_hash:
        return None
    canonical = attestation_service.canonical_json(
        attestation_service.build_document(report)
    )
    if attestation_service.content_hash(canonical) != report.content_hash:
        logger.error("report %s no longer canonicalises to %s",
                     report.id, report.content_hash)
        return None
    return canonical


def _mirrors_for(report: AnalysisReport, log) -> list[str]:
    """Where a verifier can clone the attestation log to check this report.

    The answer is the mirror list this deployment is configured with now, not
    the one that accepted the push when the report was published: those are
    never recorded, and a reader asking where to get the log wants somewhere
    reachable today rather than wherever it lived a year ago. That a mirror
    accepted it at all is what attestation_status already says.

    A report whose commit never left this machine advertises nothing. Sending
    a reader to a mirror the proof is not in wastes their time and looks like
    a broken proof; verify_commands degrades to a placeholder clone URL, which
    says plainly that the log is not published anywhere.
    """
    if log is None or report.attestation_status != "mirrored":
        return []
    # Copied so the response cannot alias the log's own list.
    return list(log.remotes)


@router.get("/reports/{report_id}/attestation", response_model=AttestationOut)
def report_attestation(report_id: str, db: Session = Depends(get_db)):
    """Everything needed to verify this report without trusting SkillChain.

    The canonical bytes, their hash, the signed commit that carries them, the
    Bitcoin timestamp proof, and the commands to check all of it. A reader who
    runs them is trusting git and Bitcoin, not us — which is the whole claim.
    """
    report = _published_report(db, report_id)
    log = _log()
    canonical = _canonical_bytes(report)
    mirrors = _mirrors_for(report, log) or []

    proof = log.read_proof(report.content_hash) if log and report.content_hash else None

    commands = []
    if report.content_hash and report.attestation_commit_sha and canonical is not None:
        commands = attestation_service.verify_commands(
            AttestationRecord(
                content_hash=report.content_hash,
                commit_sha=report.attestation_commit_sha,
                path=attestation_service.report_path(report.content_hash),
                canonical_json=canonical,
                signed=True,
                attested_at=report.attested_at,
                mirrors=mirrors,
            )
        )

    return AttestationOut(
        report_id=report.id,
        content_hash=report.content_hash,
        commit_sha=report.attestation_commit_sha,
        status=report.attestation_status,
        attested_at=report.attested_at,
        timestamp_status=report.timestamp_status,
        anchored_at=report.anchored_at,
        bitcoin_block_height=report.bitcoin_block_height,
        canonical_json=canonical.decode("utf-8") if canonical is not None else None,
        ots_proof=base64.b64encode(proof).decode("ascii") if proof else None,
        mirrors=mirrors,
        verify_commands=commands,
    )
