import re
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, HttpUrl,
    StringConstraints, field_validator,
)

from models.project import PROJECT_STATUSES, PROJECT_VISIBILITIES
from schemas.reports import ReportOut

GITHUB_HOSTS = {"github.com", "www.github.com"}
# GitHub account names: letters, digits and single hyphens, not starting or
# ending with a hyphen, at most 39 characters.
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
# Repository names: letters, digits, '.', '-' and '_', at most 100 characters.
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_SSH_RE = re.compile(r"^git@github\.com:(?P<path>.+)$", re.IGNORECASE)


def parse_github_repo_url(url: str) -> tuple[str, str]:
    """Return (owner, repo) for a GitHub repository URL, or raise ValueError.

    Accepts the forms people actually paste: http or https, with or without a
    scheme or www, a trailing slash or .git suffix, a query string or fragment,
    and SSH clone URLs. Rejects other hosts, embedded credentials, and anything
    deeper than the repository root such as /tree/main, so a submission always
    refers to the whole repository.
    """
    raw = url.strip()
    ssh = _SSH_RE.match(raw)
    if ssh:
        path = ssh.group("path")
    else:
        if "://" not in raw:
            raw = "https://" + raw
        parts = urlsplit(raw)
        if parts.scheme not in ("http", "https"):
            raise ValueError("use an https:// GitHub repository URL")
        if parts.username or parts.password:
            # Usually a pasted token; refuse rather than store it.
            raise ValueError("remove the credentials from the repository URL")
        if (parts.hostname or "").lower() not in GITHUB_HOSTS:
            raise ValueError("only GitHub repositories are supported")
        path = parts.path

    segments = path.strip("/").split("/")
    if len(segments) != 2:
        raise ValueError("use the repository's root URL, like https://github.com/owner/repo")
    owner, repo = segments
    if repo.lower().endswith(".git"):
        repo = repo[: -len(".git")]
    if not _OWNER_RE.match(owner):
        raise ValueError(f"'{owner}' is not a valid GitHub account name")
    if not _REPO_RE.match(repo) or repo in (".", ".."):
        raise ValueError(f"'{repo}' is not a valid GitHub repository name")
    return owner, repo


# Skill names are interpolated into the analysis prompt, so they are limited to
# short identifiers: no newlines or quotes to smuggle instructions through.
# Allows names like C++, C#, Node.js, CI/CD and "Go (Golang)".
SkillName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=50,
                      pattern=r"^[\w][\w +#./()&-]*$"),
]

# HttpUrl restricts the scheme to http and https, which keeps javascript: and
# data: URLs out of links the frontend will render; stored as a plain string.
HttpUrlStr = Annotated[HttpUrl, AfterValidator(str)]

MAX_SKILLS_CLAIMED = 15


class ProjectCreate(BaseModel):
    """Request body for submitting a repository for analysis."""

    # Unknown fields are rejected so clients cannot set server-owned columns
    # such as status or user_id by including them in the body.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    github_repo_url: str = Field(max_length=300)
    live_demo_url: HttpUrlStr | None = None
    skills_claimed: list[SkillName] = Field(min_length=1, max_length=MAX_SKILLS_CLAIMED)
    role_in_project: str | None = Field(default=None, max_length=500)
    visibility: Literal[PROJECT_VISIBILITIES] = "private"

    @field_validator("github_repo_url")
    @classmethod
    def _canonical_repo_url(cls, url: str) -> str:
        owner, repo = parse_github_repo_url(url)
        return f"https://github.com/{owner}/{repo}"

    @field_validator("skills_claimed")
    @classmethod
    def _dedupe_skills(cls, skills: list[str]) -> list[str]:
        # "React", "react" and "React  " are one claim; the first spelling wins.
        seen, unique = set(), []
        for skill in skills:
            skill = " ".join(skill.split())
            if skill.casefold() not in seen:
                seen.add(skill.casefold())
                unique.append(skill)
        return unique

    @property
    def repo_owner(self) -> str:
        return parse_github_repo_url(self.github_repo_url)[0]

    @property
    def repo_name(self) -> str:
        return parse_github_repo_url(self.github_repo_url)[1]


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    description: str | None
    github_repo_url: str
    repo_owner: str
    repo_name: str
    live_demo_url: str | None
    skills_claimed: Annotated[list[str], BeforeValidator(lambda v: v or [])]
    role_in_project: str | None
    status: Literal[PROJECT_STATUSES]
    # Why the pipeline stopped, when status is 'failed'.
    error_message: str | None
    visibility: Literal[PROJECT_VISIBILITIES]
    submitted_at: datetime | None


class ProjectDetailOut(ProjectOut):
    """A project with its most recent completed report, if there is one yet."""

    latest_report: ReportOut | None = None
