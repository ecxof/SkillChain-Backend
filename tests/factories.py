"""Builders for the object graph the route tests need.

A report is four joined rows deep — project, snapshot, report, assessments —
and almost every route test needs one in some particular state. Building them
here keeps each test about the behaviour it is checking.
"""

import uuid
from datetime import datetime, timezone

from models.analysis_report import AnalysisReport
from models.project import Project
from models.repo_snapshot import RepoSnapshot
from models.skill_assessment import SkillAssessment


def make_project(session, user, *, status="pending", visibility="private",
                 title="Portfolio", repo="portfolio", **extra):
    project = Project(
        id=str(uuid.uuid4()),
        user_id=user.id,
        title=title,
        github_repo_url=f"https://github.com/ada/{repo}",
        repo_owner="ada",
        repo_name=repo,
        skills_claimed=["Python", "FastAPI"],
        status=status,
        visibility=visibility,
        submitted_at=datetime.now(timezone.utc),
        **extra,
    )
    session.add(project)
    session.commit()
    return project


def make_report(session, project, *, created_at=None, verified=True,
                content_hash=None, **extra):
    """A completed analysis for ``project``, with one skill assessment."""
    snapshot = RepoSnapshot(
        id=str(uuid.uuid4()),
        project_id=project.id,
        commit_sha="a" * 40,
        default_branch="main",
        fetch_mode="public",
        fetched_at=datetime.now(timezone.utc),
    )
    report = AnalysisReport(
        id=str(uuid.uuid4()),
        repo_snapshot_id=snapshot.id,
        model_name="grok-3",
        prompt_version="v2",
        overall_trust_score=82,
        summary="Solid FastAPI service with tests.",
        authorship_signals={"commits_analyzed": 41, "signals": []},
        content_hash=content_hash,
        created_at=created_at or datetime.now(timezone.utc),
        **extra,
    )
    assessment = SkillAssessment(
        id=str(uuid.uuid4()),
        report_id=report.id,
        skill_name="Python",
        claimed=True,
        verified=verified,
        level="advanced",
        confidence=88,
        reason="Type hints and asyncio used throughout.",
        evidence=[{
            "type": "source", "detail": "Async pipeline orchestration.",
            "file": "services/analysis_service.py", "start_line": 40, "end_line": 72,
        }],
    )
    session.add_all([snapshot, report, assessment])
    session.commit()
    return report


def completed_project(session, user, **kwargs):
    """A project that finished analysis, with its report. Returns both."""
    report_kwargs = {
        key: kwargs.pop(key) for key in ("content_hash", "attestation_status",
                                         "attestation_commit_sha", "timestamp_status")
        if key in kwargs
    }
    project = make_project(session, user, status="completed", **kwargs)
    report = make_report(session, project, **report_kwargs)
    session.refresh(project)
    return project, report
