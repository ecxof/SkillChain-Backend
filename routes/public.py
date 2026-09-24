"""The unauthenticated surface: what a recruiter can read without an account.

Nothing here requires a token, so every endpoint has to decide for itself
what it is allowed to show. Two gates do that work. A profile is served only
when its owner published it; a report is served only when the project behind
it is public and finished.

Anything that fails a gate is reported as missing, never as forbidden. A
recruiter following a stale link and a stranger guessing at ids get the same
answer, so the API never confirms that a private project exists.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from db.database import get_db
from models.analysis_report import AnalysisReport
from models.project import Project
from models.repo_snapshot import RepoSnapshot
from schemas.profiles import PublicProfileOut
from schemas.reports import ReportOut
from services.profile_service import get_public_profile

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
