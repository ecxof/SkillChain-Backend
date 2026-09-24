"""The authenticated project surface: submit, follow, re-run, delete.

Analysis is slow — a repository fetch, a model call, a signed commit — so
submission schedules the work and returns at once. The client follows the run
by polling the project, whose status carries it from 'pending' through to
'completed' or 'failed'.

Every endpoint here is scoped to the caller. A project belonging to someone
else is reported as missing rather than forbidden, so the API never confirms
an id exists to someone with no right to it.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from db.database import get_db
from models.project import Project
from models.user import User
from schemas.projects import ProjectCreate, ProjectDetailOut, ProjectOut
from schemas.reports import ReportOut
from services import analysis_service
from services.auth_service import get_current_user
from services.profile_service import newest_report

router = APIRouter()

# A project in one of these is already being worked on, so scheduling it again
# would run two pipelines over one row and race them into the same columns.
IN_FLIGHT = ("pending", "fetching", "analyzing", "attesting")


def _owned(db: Session, project_id: str, user: User) -> Project:
    """The caller's project, or 404.

    Deliberately not 403 for someone else's project: telling an unauthorised
    caller that an id exists is itself a disclosure.
    """
    project = db.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return project


def _schedule(tasks: BackgroundTasks, project_id: str) -> None:
    """Queue the pipeline for a project that is already committed.

    Resolved through the module rather than imported by name so a test can
    substitute the runner; the real one reaches the network and a model.
    """
    tasks.add_task(analysis_service.run_pipeline, project_id)


def _detail(project: Project) -> ProjectDetailOut:
    report = newest_report(project)
    return ProjectDetailOut(
        **ProjectOut.model_validate(project).model_dump(),
        latest_report=ReportOut.from_report(report) if report is not None else None,
    )


@router.post("", response_model=ProjectOut, status_code=status.HTTP_202_ACCEPTED)
def submit_project(
    body: ProjectCreate,
    tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Accept a repository for analysis and start work on it.

    202 rather than 201: the project row exists, but the thing the submitter
    actually wants does not yet. They poll GET /projects/{id} for it.
    """
    project = Project(
        user_id=current_user.id,
        title=body.title,
        description=body.description,
        github_repo_url=body.github_repo_url,
        repo_owner=body.repo_owner,
        repo_name=body.repo_name,
        live_demo_url=body.live_demo_url,
        skills_claimed=body.skills_claimed,
        role_in_project=body.role_in_project,
        visibility=body.visibility,
        status="pending",
    )
    db.add(project)
    # Committed before scheduling: the pipeline opens its own session and
    # would not see a row still held in this request's transaction.
    db.commit()
    db.refresh(project)

    _schedule(tasks, project.id)
    return ProjectOut.model_validate(project)


@router.get("", response_model=list[ProjectOut])
def my_projects(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The caller's projects, most recently submitted first."""
    projects = (
        db.query(Project)
        .filter(Project.user_id == current_user.id)
        .order_by(Project.submitted_at.desc(), Project.id)
        .all()
    )
    return [ProjectOut.model_validate(project) for project in projects]


@router.get("/{project_id}", response_model=ProjectDetailOut)
def project_detail(
    project_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """One project, its current status, and its newest report if it has one.

    This is the polling endpoint: the report field stays null until the
    pipeline reaches 'completed'.
    """
    return _detail(_owned(db, project_id, current_user))


@router.get("/{project_id}/report", response_model=ReportOut)
def project_report(
    project_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The full report: verdicts, span evidence, authorship signals, provenance.

    Owners see their own reports whether or not the project is published.
    """
    project = _owned(db, project_id, current_user)
    report = newest_report(project)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This project has no report yet")
    return ReportOut.from_report(report)


@router.post("/{project_id}/reanalyze", response_model=ProjectOut,
             status_code=status.HTTP_202_ACCEPTED)
def reanalyze_project(
    project_id: str,
    tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Run the analysis again against whatever the repository looks like now.

    A fresh snapshot and a fresh report; the previous ones stay, so the record
    of what was said about an earlier commit survives.
    """
    project = _owned(db, project_id, current_user)
    if project.status in IN_FLIGHT:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This project is already being analysed")
    project.status = "pending"
    project.error_message = None
    db.commit()
    db.refresh(project)

    _schedule(tasks, project.id)
    return ProjectOut.model_validate(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a project, its snapshots and its reports.

    Reports already published to the attestation log are not withdrawn by
    this: the log is append-only, and a report that could be deleted from it
    on request would prove nothing. What a visitor loses is the route from
    the profile to it.
    """
    project = _owned(db, project_id, current_user)
    db.delete(project)
    db.commit()
