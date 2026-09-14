import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from models.skill_assessment import SKILL_LEVELS

# Slugs become frontend URLs, so words the frontend is likely to route on are
# reserved to keep a profile from shadowing a page.
RESERVED_SLUGS = frozenset({
    "admin", "api", "auth", "dashboard", "login", "logout", "me", "new",
    "profile", "profiles", "project", "projects", "public", "report", "reports",
    "settings", "signup", "skillchain", "verify",
})
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,37}[a-z0-9])$")


class ProfileSettingsUpdate(BaseModel):
    """Request body for choosing a profile URL and making the profile public."""

    model_config = ConfigDict(extra="forbid")

    profile_slug: str | None = None
    profile_is_public: bool | None = None

    @field_validator("profile_slug")
    @classmethod
    def _valid_slug(cls, slug: str | None) -> str | None:
        if slug is None:
            return None
        slug = slug.strip().lower()
        if not _SLUG_RE.match(slug) or "--" in slug:
            raise ValueError(
                "use 3-39 lowercase letters, digits and single hyphens, "
                "not starting or ending with a hyphen"
            )
        if slug in RESERVED_SLUGS:
            raise ValueError(f"'{slug}' is reserved")
        return slug


class ProfileSkillOut(BaseModel):
    skill_name: str
    # The highest verified level across the user's public reports.
    highest_level: Literal[SKILL_LEVELS] | None
    projects_evidenced: int = Field(ge=1)
    # Reports a visitor can open to see the evidence for themselves.
    report_ids: list[str]


class PublicProfileOut(BaseModel):
    slug: str
    display_name: str | None
    github_username: str | None
    avatar_url: str | None
    skills: list[ProfileSkillOut]
