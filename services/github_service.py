"""GitHub repository ingestion for the analysis pipeline.

One call, :func:`fetch_repository`, gathers everything a ``RepoSnapshot`` row
needs: repository metadata, language byte counts, the commit count, recent
commit history, per-contributor statistics, the README, the file tree and a
bounded sample of source files with their full text. Everything is pinned to
the head commit of the default branch, so a later run against the same commit
sees the same inputs, and evidence line ranges keep resolving after the
repository has moved on or disappeared.

Two access modes. With the submitter's OAuth token (``authenticated``) GitHub
allows 5000 requests an hour and serves whatever that account can see; without
one (``public``) the limit is 60 an hour per address and only public
repositories are visible. Failures the pipeline should show to the user rather
than treat as bugs are raised as :class:`GitHubError` subclasses.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import parse_qs, quote, urlsplit

import httpx

logger = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "SkillChain"
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Bounds on what one snapshot may hold, which is what bounds LLM cost.
MAX_SAMPLED_FILES = 15
MAX_SAMPLED_BYTES = 100_000
# Manifests are taken ahead of source files; a monorepo can hold dozens, so
# only the shallowest few are kept to leave room for actual code.
MAX_MANIFESTS = 5
MAX_TREE_ENTRIES = 10_000
MAX_README_CHARS = 100_000
COMMIT_HISTORY_LIMIT = 100

# GitHub answers 202 with an empty body while it computes contributor
# statistics for a repository it has not seen recently. The request is sent
# once at the start so the computation runs while everything else is fetched,
# then polled with these delays; if it is still not ready the run proceeds
# without statistics rather than failing.
STATS_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
SERVER_ERROR_RETRY_DELAY = 1.0

RATE_LIMITS = {"authenticated": 5000, "public": 60}


# --- Errors ------------------------------------------------------------------

class GitHubError(Exception):
    """A failure talking to GitHub that the user should see, not a bug."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class RepositoryNotFound(GitHubError):
    """GitHub answered 404.

    Without credentials GitHub answers 404 for private repositories as well,
    so it cannot say whether the repository is missing or merely invisible;
    ``may_be_private`` is True in that case.
    """

    def __init__(self, repo: str, *, may_be_private: bool):
        message = f"GitHub has no repository at {repo}"
        if may_be_private:
            message += (", or it is private. Link your GitHub account to analyse"
                        " private repositories")
        super().__init__(message, status_code=404)
        self.repo = repo
        self.may_be_private = may_be_private


class EmptyRepository(GitHubError):
    """The repository has no commits, so there is nothing to analyse."""


class BadCredentials(GitHubError):
    """The stored GitHub token was rejected; the user needs to sign in again."""


class RateLimited(GitHubError):
    """The API rate limit for this mode is exhausted until ``reset_at``."""

    def __init__(self, *, reset_at: datetime | None, authenticated: bool):
        mode = "authenticated" if authenticated else "public"
        message = (f"GitHub's API rate limit for {mode} requests"
                   f" ({RATE_LIMITS[mode]} per hour) is exhausted")
        if reset_at is not None:
            message += f"; it resets at {reset_at:%H:%M UTC}"
        if not authenticated:
            message += ". Link your GitHub account for a 5000 per hour limit"
        super().__init__(message, status_code=403)
        self.reset_at = reset_at
        self.authenticated = authenticated


# --- Client ------------------------------------------------------------------

class GitHubClient:
    """Thin async wrapper over the GitHub REST API with typed failures.

    ``sleep`` is awaited between retries; tests pass a stub so backoff takes no
    real time.
    """

    def __init__(self, token: str | None = None, *, sleep=asyncio.sleep):
        self.authenticated = bool(token)
        self.sleep = sleep
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset_at: datetime | None = None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # Renamed repositories answer 301; following it keeps them analysable.
        self._http = httpx.AsyncClient(
            base_url=GITHUB_API_URL, headers=headers, timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        await self.aclose()

    async def aclose(self):
        await self._http.aclose()

    async def get(
        self, path: str, *, params: dict | None = None, raw: bool = False,
        optional: bool = False, ok: tuple[int, ...] = (200,),
    ) -> httpx.Response | None:
        """GET ``path``, returning the response or raising a typed error.

        ``raw`` asks for the file bytes instead of a JSON envelope. With
        ``optional`` a 404 returns None instead of raising, for resources such
        as the README that a repository may simply not have.
        """
        headers = {"Accept": "application/vnd.github.raw+json"} if raw else None
        response = await self._http.get(path, params=params, headers=headers)
        if response.status_code >= 500:
            await self.sleep(SERVER_ERROR_RETRY_DELAY)
            response = await self._http.get(path, params=params, headers=headers)
        self._record_rate_limit(response)
        if response.status_code in ok:
            return response
        if response.status_code == 404 and optional:
            return None
        raise self._error_for(path, response)

    def _record_rate_limit(self, response: httpx.Response) -> None:
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is None or not remaining.isdigit():
            return
        self.rate_limit_remaining = int(remaining)
        self.rate_limit_reset_at = _epoch(response.headers.get("X-RateLimit-Reset"))
        if self.rate_limit_remaining < 10:
            logger.warning("GitHub rate limit nearly exhausted: %s requests left until %s",
                           self.rate_limit_remaining, self.rate_limit_reset_at)

    def _error_for(self, path: str, response: httpx.Response) -> GitHubError:
        status = response.status_code
        message = _error_message(response)
        if status == 401:
            return BadCredentials(f"GitHub rejected the stored access token: {message}",
                                  status_code=401)
        if status in (403, 429) and _looks_rate_limited(response, message):
            return RateLimited(reset_at=_reset_time(response), authenticated=self.authenticated)
        if status == 404:
            return RepositoryNotFound(_repo_from_path(path), may_be_private=not self.authenticated)
        if status == 409 and "empty" in message.lower():
            return EmptyRepository(f"The repository {_repo_from_path(path)} has no commits yet",
                                   status_code=409)
        return GitHubError(f"GitHub returned {status} for {path}: {message}", status_code=status)


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])
    return response.reason_phrase or f"HTTP {response.status_code}"


def _looks_rate_limited(response: httpx.Response, message: str) -> bool:
    if response.headers.get("X-RateLimit-Remaining") == "0":
        return True
    if "Retry-After" in response.headers:
        return True
    return "rate limit" in message.lower()


def _reset_time(response: httpx.Response) -> datetime | None:
    retry_after = response.headers.get("Retry-After")
    if retry_after and retry_after.isdigit():
        return datetime.now(timezone.utc) + timedelta(seconds=int(retry_after))
    return _epoch(response.headers.get("X-RateLimit-Reset"))


def _epoch(value: str | None) -> datetime | None:
    if value and value.isdigit():
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    return None


_REPO_PATH = re.compile(r"^/repos/([^/]+)/([^/]+)")


def _repo_from_path(path: str) -> str:
    match = _REPO_PATH.match(path)
    return f"{match.group(1)}/{match.group(2)}" if match else path


# --- File sampling -----------------------------------------------------------

@dataclass(frozen=True)
class TreeEntry:
    path: str
    size: int


# Always sampled: they state the stack directly and are small.
MANIFEST_NAMES = frozenset({
    "package.json", "requirements.txt", "pyproject.toml", "setup.py", "Pipfile",
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts",
    "composer.json", "Gemfile", "Dockerfile", "docker-compose.yml",
    "docker-compose.yaml", "compose.yml", "compose.yaml", "Makefile",
    "CMakeLists.txt",
})

SOURCE_EXTENSIONS = frozenset({
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".groovy", ".rb", ".php",
    ".cs", ".fs", ".vb", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh",
    ".swift", ".m", ".mm", ".dart", ".ex", ".exs", ".erl", ".hs", ".ml", ".clj",
    ".cljs", ".elm", ".lua", ".r", ".jl", ".pl", ".pm", ".sh", ".bash", ".zsh",
    ".ps1", ".psm1", ".sql", ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".graphql", ".gql", ".proto", ".sol", ".zig", ".nim",
})

# Dependencies, build output and caches: never the submitter's own work.
SKIPPED_DIRECTORIES = frozenset({
    "node_modules", "vendor", "dist", "build", "target", "out", "obj",
    "__pycache__", ".venv", "venv", "site-packages", "coverage", ".next",
    ".nuxt", "bower_components", "third_party", "Pods", ".terraform", ".tox",
    ".git",
})

LOCKFILES = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "bun.lockb", "poetry.lock", "Pipfile.lock", "uv.lock", "pdm.lock",
    "Cargo.lock", "composer.lock", "Gemfile.lock", "go.sum",
    "packages.lock.json", "mix.lock", "pubspec.lock", "flake.lock",
    "Podfile.lock", "gradle.lockfile",
})

# Minified bundles and generated code say nothing about the author.
GENERATED_MARKERS = (
    ".min.", "_pb2.py", "_pb2_grpc.py", ".pb.go", ".g.dart", ".freezed.dart",
    ".generated.", ".designer.cs",
)


def is_manifest(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if name in MANIFEST_NAMES or name.startswith("Dockerfile.") or name.endswith(".tf"):
        return True
    return path.startswith(".github/workflows/") and name.endswith((".yml", ".yaml"))


def is_source(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return dot > 0 and name[dot:].lower() in SOURCE_EXTENSIONS


def is_excluded(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    if any(part in SKIPPED_DIRECTORIES for part in parts[:-1]):
        return True
    return name in LOCKFILES or any(marker in name for marker in GENERATED_MARKERS)


def select_files(
    entries: Iterable[TreeEntry], *, max_files: int = MAX_SAMPLED_FILES,
    max_bytes: int = MAX_SAMPLED_BYTES,
) -> list[TreeEntry]:
    """Choose which files to fetch: manifests first, then the largest sources.

    Larger source files carry more evidence per request. A file that does not
    fit in the remaining byte budget is skipped rather than ending the
    selection, so one huge file does not crowd out several smaller ones.
    """
    candidates = [e for e in entries if e.size > 0 and not is_excluded(e.path)]
    manifests = sorted((e for e in candidates if is_manifest(e.path)),
                       key=lambda e: (e.path.count("/"), e.path))[:MAX_MANIFESTS]
    sources = sorted((e for e in candidates if not is_manifest(e.path) and is_source(e.path)),
                     key=lambda e: (-e.size, e.path))
    selected: list[TreeEntry] = []
    used = 0
    for entry in [*manifests, *sources]:
        if len(selected) >= max_files:
            break
        if used + entry.size > max_bytes:
            continue
        selected.append(entry)
        used += entry.size
    return selected


# --- Fetching ----------------------------------------------------------------

@dataclass
class RepositoryData:
    """Everything fetched for one analysis run, pinned to ``commit_sha``."""

    owner: str
    name: str
    full_name: str
    description: str | None
    html_url: str
    is_private: bool
    default_branch: str
    commit_sha: str
    languages: dict[str, int]
    topics: list[str]
    stars: int
    forks: int
    commit_count: int | None
    # Commits by the submitter, from contributor statistics; None when GitHub
    # had not finished computing them or no submitter was named.
    user_commit_count: int | None
    readme_content: str | None
    # {"truncated": bool, "entry_count": int, "entries": [{"path", "size"}]}
    file_tree: dict
    # {path: full text}, manifests first, then source files largest first.
    sampled_files: dict[str, str]
    commit_history: list[dict]
    contributor_stats: list[dict] | None
    fetch_mode: str
    fetched_at: datetime

    def snapshot_fields(self) -> dict:
        """The ``RepoSnapshot`` columns: ``RepoSnapshot(project_id=..., **fields)``."""
        return {
            "commit_sha": self.commit_sha,
            "default_branch": self.default_branch,
            "languages": self.languages,
            "topics": self.topics,
            "stars": self.stars,
            "forks": self.forks,
            "commit_count": self.commit_count,
            "user_commit_count": self.user_commit_count,
            "readme_content": self.readme_content,
            "file_tree": self.file_tree,
            "sampled_files": self.sampled_files,
            "commit_history": self.commit_history,
            "contributor_stats": self.contributor_stats,
            "fetch_mode": self.fetch_mode,
            "fetched_at": self.fetched_at,
        }


async def fetch_repository(
    owner: str, name: str, *, token: str | None = None,
    submitter_login: str | None = None, sleep=asyncio.sleep,
) -> RepositoryData:
    """Fetch ``owner/name`` from GitHub, with ``token`` when the user linked one.

    ``submitter_login`` is the user's GitHub username, used to count their own
    commits against the whole repository's.
    """
    async with GitHubClient(token, sleep=sleep) as github:
        return await _fetch(github, owner, name, submitter_login)


async def _fetch(github: GitHubClient, owner: str, name: str,
                 submitter_login: str | None) -> RepositoryData:
    base = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
    meta = (await github.get(base)).json()
    # Sent first so GitHub starts computing while the rest is fetched.
    stats = await _contributor_stats(github, base)

    default_branch = meta["default_branch"]
    commits = await _commit_history(github, base, default_branch)
    head = commits[0]["sha"]
    commit_count = await _commit_count(github, base, head)

    languages = (await github.get(f"{base}/languages")).json()
    readme = await _readme(github, base, head)
    entries, truncated = await _tree(github, base, head)
    sampled = await _sampled_files(github, base, head, entries)

    for delay in STATS_RETRY_DELAYS:
        if stats is not None:
            break
        await github.sleep(delay)
        stats = await _contributor_stats(github, base)
    if stats is None:
        logger.info("Contributor statistics for %s/%s were not ready; continuing without them",
                    owner, name)

    return RepositoryData(
        owner=owner,
        name=name,
        full_name=meta.get("full_name") or f"{owner}/{name}",
        description=meta.get("description"),
        html_url=meta.get("html_url") or f"https://github.com/{owner}/{name}",
        is_private=bool(meta.get("private")),
        default_branch=default_branch,
        commit_sha=head,
        languages=languages if isinstance(languages, dict) else {},
        topics=list(meta.get("topics") or []),
        stars=int(meta.get("stargazers_count") or 0),
        forks=int(meta.get("forks_count") or 0),
        commit_count=commit_count,
        user_commit_count=_submitter_commits(stats, submitter_login),
        readme_content=readme,
        file_tree={
            "truncated": truncated or len(entries) > MAX_TREE_ENTRIES,
            "entry_count": len(entries),
            "entries": [{"path": e.path, "size": e.size} for e in entries[:MAX_TREE_ENTRIES]],
        },
        sampled_files=sampled,
        commit_history=[_trim_commit(c) for c in commits],
        contributor_stats=stats,
        fetch_mode="authenticated" if github.authenticated else "public",
        fetched_at=datetime.now(timezone.utc),
    )


async def _commit_history(github: GitHubClient, base: str, branch: str) -> list[dict]:
    response = await github.get(f"{base}/commits",
                                params={"sha": branch, "per_page": COMMIT_HISTORY_LIMIT})
    commits = response.json()
    if not commits:
        raise EmptyRepository(f"The repository {_repo_from_path(base)} has no commits yet")
    return commits


async def _commit_count(github: GitHubClient, base: str, head: str) -> int:
    """Total commits reachable from ``head``, read off the last page number."""
    response = await github.get(f"{base}/commits", params={"sha": head, "per_page": 1})
    return parse_last_page(response.headers.get("Link")) or len(response.json())


_LAST_LINK = re.compile(r'<([^>]+)>;\s*rel="last"')


def parse_last_page(link_header: str | None) -> int | None:
    """The page number of the ``rel="last"`` link, or None if there is none."""
    match = _LAST_LINK.search(link_header or "")
    if not match:
        return None
    page = parse_qs(urlsplit(match.group(1)).query).get("page", [""])[0]
    return int(page) if page.isdigit() else None


async def _contributor_stats(github: GitHubClient, base: str) -> list[dict] | None:
    response = await github.get(f"{base}/stats/contributors", ok=(200, 202, 204))
    if response.status_code != 200:
        return None
    stats = response.json()
    if not isinstance(stats, list):
        return None
    return [_trim_contributor(c) for c in stats]


def _trim_contributor(contributor: dict) -> dict:
    """Keep the login, the total and only the weeks with any activity."""
    author = contributor.get("author") or {}
    weeks = [
        {"w": w.get("w"), "a": w.get("a", 0), "d": w.get("d", 0), "c": w.get("c", 0)}
        for w in contributor.get("weeks") or []
        if w.get("a") or w.get("d") or w.get("c")
    ]
    return {"login": author.get("login"), "total": contributor.get("total", 0), "weeks": weeks}


def _submitter_commits(stats: list[dict] | None, login: str | None) -> int | None:
    if stats is None or not login:
        return None
    return sum(c["total"] for c in stats if (c.get("login") or "").lower() == login.lower())


def _trim_commit(commit: dict) -> dict:
    """The fields the authorship signals need; author emails are left out."""
    details = commit.get("commit") or {}
    author = details.get("author") or {}
    committer = details.get("committer") or {}
    verification = details.get("verification") or {}
    return {
        "sha": commit.get("sha"),
        "message": details.get("message") or "",
        "author_name": author.get("name"),
        "author_login": (commit.get("author") or {}).get("login"),
        "authored_at": author.get("date"),
        "committed_at": committer.get("date"),
        "parent_count": len(commit.get("parents") or []),
        "signature_verified": bool(verification.get("verified")),
    }


async def _readme(github: GitHubClient, base: str, ref: str) -> str | None:
    response = await github.get(f"{base}/readme", params={"ref": ref}, raw=True, optional=True)
    if response is None:
        return None
    text = response.content.decode("utf-8", errors="replace")
    if len(text) > MAX_README_CHARS:
        text = text[:MAX_README_CHARS] + "\n[README truncated]\n"
    return text


async def _tree(github: GitHubClient, base: str, ref: str) -> tuple[list[TreeEntry], bool]:
    """Every file in the tree at ``ref``; symlinks and submodules are left out."""
    response = await github.get(f"{base}/git/trees/{ref}", params={"recursive": "1"})
    data = response.json()
    entries = [
        TreeEntry(item["path"], int(item.get("size") or 0))
        for item in data.get("tree") or []
        if item.get("type") == "blob" and item.get("mode") != "120000"
    ]
    return entries, bool(data.get("truncated"))


async def _sampled_files(github: GitHubClient, base: str, ref: str,
                         entries: list[TreeEntry]) -> dict[str, str]:
    """Fetch the selected files' full text, skipping anything not UTF-8."""
    sampled: dict[str, str] = {}
    for entry in select_files(entries):
        response = await github.get(f"{base}/contents/{quote(entry.path, safe='/')}",
                                    params={"ref": ref}, raw=True, optional=True)
        if response is None:
            continue
        content = response.content
        if b"\x00" in content:
            continue
        try:
            sampled[entry.path] = content.decode("utf-8")
        except UnicodeDecodeError:
            logger.info("Skipping %s: not UTF-8 text", entry.path)
    return sampled
