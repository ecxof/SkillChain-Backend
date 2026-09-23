import os
import uuid
from datetime import datetime, timedelta, timezone

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
from models.skill_assessment import SkillAssessment  # noqa: E402
from models.user import User  # noqa: E402
from services.profile_service import build_profile, get_public_profile  # noqa: E402

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


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
def user(session):
    user = User(id=str(uuid.uuid4()), email="octocat@example.test",
                github_username="octocat", display_name="The Octocat",
                avatar_url="https://avatars.example/octocat.png",
                profile_slug="octocat", profile_is_public=True)
    session.add(user)
    session.commit()
    return user


def add_project(session, user, skills, *, visibility="public", status="completed",
                created_at=NOW, title="Portfolio"):
    """One project with a completed report asserting ``skills``.

    ``skills`` is a list of (name, verified, level) tuples.
    """
    project = Project(
        id=str(uuid.uuid4()), user_id=user.id, title=title,
        github_repo_url=f"https://github.com/octocat/{title.lower()}",
        repo_owner="octocat", repo_name=title.lower(),
        status=status, visibility=visibility,
    )
    snapshot = RepoSnapshot(id=str(uuid.uuid4()), project_id=project.id,
                            commit_sha="a" * 40, fetch_mode="public")
    report = AnalysisReport(id=str(uuid.uuid4()), repo_snapshot_id=snapshot.id,
                            model_name="grok-3", prompt_version="v2",
                            overall_trust_score=80, created_at=created_at)
    session.add_all([project, snapshot, report])
    for name, verified, level in skills:
        session.add(SkillAssessment(
            id=str(uuid.uuid4()), report_id=report.id, skill_name=name,
            claimed=True, verified=verified, level=level if verified else None,
            confidence=80, evidence=[],
        ))
    session.commit()
    return project, report


# --- What reaches a profile --------------------------------------------------

def test_a_profile_lists_the_verified_skills_with_the_reports_behind_them(session, user):
    _, report = add_project(session, user, [("React", True, "advanced")])

    profile = build_profile(user)

    assert (profile.slug, profile.display_name) == ("octocat", "The Octocat")
    assert profile.github_username == "octocat"
    assert len(profile.skills) == 1
    skill = profile.skills[0]
    assert (skill.skill_name, skill.highest_level) == ("React", "advanced")
    assert skill.projects_evidenced == 1
    # A visitor can open these to check the evidence themselves.
    assert skill.report_ids == [report.id]


def test_unverified_skills_do_not_reach_a_profile(session, user):
    add_project(session, user, [("React", True, "advanced"), ("Rust", False, None)])
    assert [s.skill_name for s in build_profile(user).skills] == ["React"]


def test_private_projects_never_contribute(session, user):
    add_project(session, user, [("React", True, "advanced")], visibility="private")
    # The profile being public does not make the projects behind it public.
    assert build_profile(user).skills == []


def test_unfinished_projects_do_not_contribute(session, user):
    add_project(session, user, [("React", True, "advanced")], status="analyzing")
    add_project(session, user, [("CSS", True, "beginner")], status="failed", title="Other")
    assert build_profile(user).skills == []


def test_a_project_with_no_report_yet_is_skipped(session, user):
    project = Project(id=str(uuid.uuid4()), user_id=user.id, title="Empty",
                      github_repo_url="https://github.com/octocat/empty",
                      repo_owner="octocat", repo_name="empty",
                      status="completed", visibility="public")
    session.add(project)
    session.commit()
    assert build_profile(user).skills == []


# --- Aggregation across projects ---------------------------------------------

def test_a_skill_evidenced_by_several_projects_counts_each_of_them(session, user):
    _, first = add_project(session, user, [("React", True, "intermediate")], title="One")
    _, second = add_project(session, user, [("React", True, "beginner")], title="Two")

    skill = build_profile(user).skills[0]

    assert skill.projects_evidenced == 2
    assert set(skill.report_ids) == {first.id, second.id}


def test_the_strongest_level_wins_across_projects(session, user):
    add_project(session, user, [("React", True, "beginner")], title="One")
    add_project(session, user, [("React", True, "advanced")], title="Two")
    add_project(session, user, [("React", True, "intermediate")], title="Three")

    assert build_profile(user).skills[0].highest_level == "advanced"


def test_a_reanalysed_project_counts_once_and_uses_its_newest_report(session, user):
    project, old = add_project(session, user, [("React", True, "beginner")])

    # A second run against the same project: re-analysis appends rather than
    # replacing, so the older report is still in the database.
    snapshot = RepoSnapshot(id=str(uuid.uuid4()), project_id=project.id,
                            commit_sha="b" * 40, fetch_mode="public")
    newer = AnalysisReport(id=str(uuid.uuid4()), repo_snapshot_id=snapshot.id,
                           model_name="grok-3", prompt_version="v2",
                           created_at=NOW + timedelta(days=1))
    session.add_all([snapshot, newer])
    session.add(SkillAssessment(id=str(uuid.uuid4()), report_id=newer.id,
                                skill_name="React", claimed=True, verified=True,
                                level="advanced", confidence=90, evidence=[]))
    session.commit()

    skill = build_profile(user).skills[0]
    assert skill.projects_evidenced == 1
    assert skill.report_ids == [newer.id]
    assert skill.highest_level == "advanced"


def test_skills_are_ordered_by_evidence_then_strength_then_name(session, user):
    add_project(session, user, [("React", True, "beginner"),
                                ("Zig", True, "advanced"),
                                ("Ansible", True, "advanced")], title="One")
    add_project(session, user, [("React", True, "beginner")], title="Two")

    names = [skill.skill_name for skill in build_profile(user).skills]
    # React is evidenced twice; Ansible and Zig tie on evidence and level, so
    # the name breaks it, keeping the order stable between requests.
    assert names == ["React", "Ansible", "Zig"]


# --- Visibility of the profile itself ----------------------------------------

def test_a_public_profile_is_served_by_slug(session, user):
    add_project(session, user, [("React", True, "advanced")])
    assert get_public_profile(session, "octocat").slug == "octocat"


def test_a_private_profile_is_indistinguishable_from_a_missing_one(session, user):
    add_project(session, user, [("React", True, "advanced")])
    user.profile_is_public = False
    session.commit()

    # A visitor must not be able to tell that this slug is taken.
    assert get_public_profile(session, "octocat") is None
    assert get_public_profile(session, "nobody") is None
