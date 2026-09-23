"""Aggregating a user's reports into the public skill profile.

The profile is the thing a developer shares; the reports behind it are the
evidence a reader drills into. It is computed on read rather than stored, so
there is no cached total to fall out of step with the reports it summarises.

Two gates decide what a visitor sees, and both must be open: the user has made
their profile public, and the individual project is public. A private project
never contributes to a profile, even a public one.
"""

from models.project import Project
from models.user import User
from schemas.profiles import ProfileSkillOut, PublicProfileOut

# Only verified skills reach a profile, and the strongest level wins when a
# skill is evidenced by several projects.
LEVEL_ORDER = {"beginner": 1, "intermediate": 2, "advanced": 3}


def get_public_profile(session, slug: str) -> PublicProfileOut | None:
    """The profile published at ``slug``, or None if there is nothing to show.

    None covers every case a visitor should not be able to tell apart: no such
    slug, and a profile whose owner has not made it public.
    """
    user = session.query(User).filter(User.profile_slug == slug).first()
    if user is None or not user.profile_is_public:
        return None
    return build_profile(user)


def build_profile(user: User) -> PublicProfileOut:
    """Aggregate a user's public, completed projects into one skill list."""
    skills: dict[str, dict] = {}

    for project in user.projects:
        report = latest_report(project)
        if report is None:
            continue
        for assessment in report.skills:
            if not assessment.verified:
                continue
            entry = skills.setdefault(
                assessment.skill_name,
                {"projects": set(), "report_ids": [], "level": None},
            )
            entry["projects"].add(project.id)
            if report.id not in entry["report_ids"]:
                entry["report_ids"].append(report.id)
            entry["level"] = _stronger(entry["level"], assessment.level)

    return PublicProfileOut(
        slug=user.profile_slug,
        display_name=user.display_name,
        github_username=user.github_username,
        avatar_url=user.avatar_url,
        skills=[
            ProfileSkillOut(
                skill_name=name,
                highest_level=entry["level"],
                projects_evidenced=len(entry["projects"]),
                report_ids=entry["report_ids"],
            )
            # Most widely evidenced first, then strongest, then by name, so the
            # ordering is stable and the best-supported claims lead.
            for name, entry in sorted(
                skills.items(),
                key=lambda item: (
                    -len(item[1]["projects"]),
                    -LEVEL_ORDER.get(item[1]["level"], 0),
                    item[0].casefold(),
                ),
            )
        ],
    )


def latest_report(project: Project):
    """The newest report for a public, completed project, or None.

    Re-analysis adds a report rather than replacing one, so a project with a
    history still counts once, and it counts as whatever it says now.
    """
    if project.visibility != "public" or project.status != "completed":
        return None
    reports = [
        snapshot.report for snapshot in project.snapshots if snapshot.report is not None
    ]
    if not reports:
        return None
    return max(reports, key=lambda report: (report.created_at, report.id))


def _stronger(current: str | None, candidate: str | None) -> str | None:
    if LEVEL_ORDER.get(candidate, 0) > LEVEL_ORDER.get(current, 0):
        return candidate
    return current
