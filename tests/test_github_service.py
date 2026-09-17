import asyncio
import os
import re
from datetime import datetime, timedelta, timezone

# Only the model-alignment test imports the ORM, which builds the engine on
# import; load_dotenv() never overrides a variable that is already set.
os.environ.setdefault("DATABASE_URL", "sqlite://")

import httpx  # noqa: E402
import pytest  # noqa: E402
import respx  # noqa: E402

from services.github_service import (  # noqa: E402
    MAX_README_CHARS, MAX_SAMPLED_BYTES, MAX_SAMPLED_FILES, SERVER_ERROR_RETRY_DELAY,
    STATS_RETRY_DELAYS, BadCredentials, EmptyRepository, GitHubError, RateLimited,
    RepositoryNotFound, TreeEntry, fetch_repository, is_manifest, parse_last_page,
    select_files,
)

API = "https://api.github.com"
REPO = f"{API}/repos/octocat/Hello-World"
HEAD = "a3f9c1b" + "0" * 33

METADATA = {
    "name": "Hello-World", "full_name": "octocat/Hello-World",
    "description": "My first repository on GitHub!", "private": False,
    "html_url": "https://github.com/octocat/Hello-World", "default_branch": "main",
    "stargazers_count": 3, "forks_count": 1, "topics": ["demo", "octocat"],
}


def commit(sha, message, login="octocat", parents=1, verified=False):
    return {
        "sha": sha,
        "commit": {
            "message": message,
            "author": {"name": "The Octocat", "email": "octocat@example.test",
                       "date": "2026-09-01T10:00:00Z"},
            "committer": {"name": "GitHub", "email": "noreply@github.com",
                          "date": "2026-09-01T10:05:00Z"},
            "verification": {"verified": verified},
        },
        "author": {"login": login} if login else None,
        "parents": [{"sha": "p"}] * parents,
    }


COMMITS = [
    commit(HEAD, "Add search\n\nCo-authored-by: Copilot <copilot@github.com>", verified=True),
    commit("b" * 40, "Merge pull request #1", parents=2),
    commit("c" * 40, "Initial commit", login=None),
]

LINK = (f'<{REPO}/commits?sha={HEAD}&per_page=1&page=2>; rel="next", '
        f'<{REPO}/commits?sha={HEAD}&per_page=1&page=341>; rel="last"')

STATS = [
    {"author": {"login": "octocat", "id": 1}, "total": 42,
     "weeks": [{"w": 1756684800, "a": 10, "d": 2, "c": 3},
               {"w": 1757289600, "a": 0, "d": 0, "c": 0}]},
    {"author": {"login": "other", "id": 2}, "total": 8,
     "weeks": [{"w": 1756684800, "a": 100, "d": 0, "c": 8}]},
]

FILES = {
    "package.json": '{"name": "hello", "dependencies": {"react": "18.3.1"}}\n',
    ".github/workflows/ci.yml": "name: CI\non: push\n",
    "src/App.jsx": "export default function App() {\n  return <h1>Hello</h1>;\n}\n",
    "src/index.js": "import App from './App';\n",
    "src/utils/format.ts": "export const format = (n: number) => n.toFixed(2);\n",
}


def blob(path, size):
    return {"path": path, "mode": "100644", "type": "blob", "sha": "x", "size": size}


TREE = {
    "sha": HEAD,
    "truncated": False,
    "tree": [
        {"path": "src", "mode": "040000", "type": "tree", "sha": "t"},
        blob("package.json", 400),
        blob(".github/workflows/ci.yml", 600),
        blob("README.md", 1200),
        blob("logo.png", 5000),
        blob("src/App.jsx", 3000),
        blob("src/index.js", 800),
        blob("src/utils/format.ts", 1500),
        blob("src/huge.js", 200_000),
        blob("node_modules/react/index.js", 50_000),
        blob("dist/bundle.min.js", 90_000),
        blob("package-lock.json", 200_000),
        {"path": "link", "mode": "120000", "type": "blob", "sha": "l", "size": 10},
        {"path": "sub", "mode": "160000", "type": "commit", "sha": "s"},
    ],
}


def serve_content(request, path):
    if path in FILES:
        return httpx.Response(200, content=FILES[path].encode())
    return httpx.Response(404, json={"message": "Not Found"})


def serve_commits(request):
    if request.url.params.get("per_page") == "1":
        return httpx.Response(200, json=[COMMITS[0]], headers={"Link": LINK})
    return httpx.Response(200, json=COMMITS)


@pytest.fixture
def github():
    with respx.mock(assert_all_called=False) as router:
        router.get(REPO, name="meta").respond(json=METADATA)
        router.get(f"{REPO}/languages", name="languages").respond(
            json={"JavaScript": 1000, "TypeScript": 500})
        router.get(f"{REPO}/commits", name="commits").mock(side_effect=serve_commits)
        router.get(f"{REPO}/stats/contributors", name="stats").respond(json=STATS)
        router.get(f"{REPO}/readme", name="readme").respond(text="# Hello\n")
        router.get(f"{REPO}/git/trees/{HEAD}", name="tree").respond(json=TREE)
        router.get(url__regex=rf"{re.escape(REPO)}/contents/(?P<path>[^?]+)",
                   name="contents").mock(side_effect=serve_content)
        yield router


class FakeSleep:
    """Records the delays the service asked for instead of waiting."""

    def __init__(self):
        self.delays = []

    async def __call__(self, seconds):
        self.delays.append(seconds)


def fetch(**kwargs):
    kwargs.setdefault("sleep", FakeSleep())
    return asyncio.run(fetch_repository("octocat", "Hello-World", **kwargs))


# --- The snapshot -----------------------------------------------------------

def test_public_fetch_assembles_a_snapshot_pinned_to_the_head_commit(github):
    data = fetch()

    assert data.fetch_mode == "public"
    assert (data.full_name, data.default_branch, data.commit_sha) == (
        "octocat/Hello-World", "main", HEAD)
    assert data.commit_count == 341
    assert data.languages == {"JavaScript": 1000, "TypeScript": 500}
    assert data.topics == ["demo", "octocat"]
    assert (data.stars, data.forks, data.is_private) == (3, 1, False)
    assert data.readme_content == "# Hello\n"
    assert data.user_commit_count is None
    assert data.fetched_at.tzinfo is timezone.utc


def test_sample_takes_manifests_then_the_largest_sources_and_keeps_full_text(github):
    data = fetch()

    assert list(data.sampled_files) == [
        "package.json", ".github/workflows/ci.yml",
        "src/App.jsx", "src/utils/format.ts", "src/index.js",
    ]
    assert data.sampled_files == FILES


def test_tree_is_kept_whole_without_symlinks_submodules_or_directories(github):
    tree = fetch().file_tree

    paths = [entry["path"] for entry in tree["entries"]]
    assert "node_modules/react/index.js" in paths
    assert not {"link", "sub", "src"} & set(paths)
    assert tree["truncated"] is False
    assert tree["entry_count"] == len(paths)


def test_commit_history_keeps_what_authorship_signals_need_and_drops_emails(github):
    history = fetch().commit_history

    assert history[0] == {
        "sha": HEAD,
        "message": "Add search\n\nCo-authored-by: Copilot <copilot@github.com>",
        "author_name": "The Octocat", "author_login": "octocat",
        "authored_at": "2026-09-01T10:00:00Z", "committed_at": "2026-09-01T10:05:00Z",
        "parent_count": 1, "signature_verified": True,
    }
    assert history[1]["parent_count"] == 2
    assert history[2]["author_login"] is None
    assert "octocat@example.test" not in str(history)


def test_contributor_statistics_are_trimmed_to_active_weeks(github):
    assert fetch().contributor_stats == [
        {"login": "octocat", "total": 42, "weeks": [{"w": 1756684800, "a": 10, "d": 2, "c": 3}]},
        {"login": "other", "total": 8, "weeks": [{"w": 1756684800, "a": 100, "d": 0, "c": 8}]},
    ]


def test_snapshot_fields_fill_the_repo_snapshot_model(github):
    from models.repo_snapshot import RepoSnapshot

    fields = fetch(token="gho_secret", submitter_login="octocat").snapshot_fields()

    columns = {column.name: column for column in RepoSnapshot.__table__.columns}
    assert set(fields) <= set(columns)
    required = {name for name, column in columns.items()
                if not column.nullable and column.default is None
                and column.server_default is None and not column.primary_key}
    assert required <= set(fields) | {"project_id"}
    snapshot = RepoSnapshot(project_id="p1", **fields)
    assert (snapshot.commit_sha, snapshot.fetch_mode, snapshot.user_commit_count) == (
        HEAD, "authenticated", 42)


# --- Access modes -----------------------------------------------------------

def test_public_mode_sends_no_credentials_and_pins_files_to_the_head_commit(github):
    fetch()

    requests = [call.request for call in github.calls]
    assert all("Authorization" not in r.headers for r in requests)
    assert all(r.headers["X-GitHub-Api-Version"] == "2022-11-28" for r in requests)
    contents = [r for r in requests if "/contents/" in r.url.path]
    assert len(contents) == 5
    assert all(r.url.params["ref"] == HEAD for r in contents)
    assert all(r.headers["Accept"] == "application/vnd.github.raw+json" for r in contents)
    readme = next(r for r in requests if r.url.path.endswith("/readme"))
    assert readme.url.params["ref"] == HEAD


def test_linked_token_is_sent_and_the_submitters_commits_are_counted(github):
    data = fetch(token="gho_secret", submitter_login="OctoCat")

    assert data.fetch_mode == "authenticated"
    assert data.user_commit_count == 42
    assert all(call.request.headers["Authorization"] == "Bearer gho_secret"
               for call in github.calls)


def test_a_submitter_absent_from_the_statistics_has_zero_commits(github):
    assert fetch(submitter_login="stranger").user_commit_count == 0


# --- Failures the user should see -------------------------------------------

def test_missing_repository_without_a_token_may_be_private(github):
    github["meta"].respond(404, json={"message": "Not Found"})

    with pytest.raises(RepositoryNotFound) as exc:
        fetch()
    assert exc.value.may_be_private is True
    assert "octocat/Hello-World" in str(exc.value)
    assert "Link your GitHub account" in str(exc.value)


def test_missing_repository_with_a_token_is_simply_missing(github):
    github["meta"].respond(404, json={"message": "Not Found"})

    with pytest.raises(RepositoryNotFound) as exc:
        fetch(token="gho_secret")
    assert exc.value.may_be_private is False
    assert "private" not in str(exc.value)


def test_exhausted_rate_limit_reports_when_it_resets(github):
    github["meta"].respond(
        403, json={"message": "API rate limit exceeded for 203.0.113.9."},
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1790000000"})

    with pytest.raises(RateLimited) as exc:
        fetch()
    assert exc.value.reset_at == datetime(2026, 9, 21, 14, 13, 20, tzinfo=timezone.utc)
    assert exc.value.authenticated is False
    assert "60 per hour" in str(exc.value)
    assert "Link your GitHub account" in str(exc.value)


def test_secondary_rate_limit_uses_the_retry_after_header(github):
    github["meta"].respond(
        429, json={"message": "You have exceeded a secondary rate limit."},
        headers={"Retry-After": "30", "X-RateLimit-Remaining": "4990"})

    before = datetime.now(timezone.utc)
    with pytest.raises(RateLimited) as exc:
        fetch(token="gho_secret")
    assert timedelta(seconds=29) <= exc.value.reset_at - before <= timedelta(seconds=31)
    assert exc.value.authenticated is True
    assert "5000 per hour" in str(exc.value)
    assert "Link your GitHub account" not in str(exc.value)


def test_other_forbidden_answers_are_not_mistaken_for_rate_limits(github):
    github["meta"].respond(403, json={"message": "Repository access blocked"},
                           headers={"X-RateLimit-Remaining": "4999"})

    with pytest.raises(GitHubError) as exc:
        fetch()
    assert not isinstance(exc.value, RateLimited)
    assert exc.value.status_code == 403
    assert "Repository access blocked" in str(exc.value)


def test_rejected_token_is_reported(github):
    github["meta"].respond(401, json={"message": "Bad credentials"})

    with pytest.raises(BadCredentials, match="Bad credentials"):
        fetch(token="gho_stale")


def test_an_empty_repository_is_reported(github):
    github["commits"].respond(409, json={"message": "Git Repository is empty."})

    with pytest.raises(EmptyRepository, match="no commits"):
        fetch()


# --- Degrading rather than failing ------------------------------------------

def test_contributor_statistics_are_polled_while_github_computes_them(github):
    github["stats"].mock(side_effect=[
        httpx.Response(202), httpx.Response(202), httpx.Response(200, json=STATS)])
    sleep = FakeSleep()

    data = fetch(sleep=sleep, submitter_login="octocat")

    assert data.user_commit_count == 42
    assert sleep.delays == list(STATS_RETRY_DELAYS[:2])
    assert github["stats"].call_count == 3


def test_statistics_that_never_arrive_degrade_rather_than_fail(github):
    github["stats"].respond(202)
    sleep = FakeSleep()

    data = fetch(sleep=sleep, submitter_login="octocat")

    assert data.contributor_stats is None
    assert data.user_commit_count is None
    assert sleep.delays == list(STATS_RETRY_DELAYS)
    assert github["stats"].call_count == 1 + len(STATS_RETRY_DELAYS)


def test_a_repository_without_a_readme_is_not_an_error(github):
    github["readme"].respond(404, json={"message": "Not Found"})
    assert fetch().readme_content is None


def test_an_enormous_readme_is_cut_to_size(github):
    github["readme"].respond(text="x" * (MAX_README_CHARS + 5))

    readme = fetch().readme_content

    assert readme.endswith("[README truncated]\n")
    assert len(readme) < MAX_README_CHARS + 50


def test_a_truncated_tree_is_flagged_and_still_sampled(github):
    github["tree"].respond(json={**TREE, "truncated": True})

    data = fetch()

    assert data.file_tree["truncated"] is True
    assert "package.json" in data.sampled_files


def test_files_that_are_not_text_are_left_out_of_the_sample(github):
    def serve(request, path):
        if path == "src/App.jsx":
            return httpx.Response(200, content=b"\xff\xfe\x00\x01binary")
        return serve_content(request, path)
    github["contents"].mock(side_effect=serve)

    data = fetch()

    assert "src/App.jsx" not in data.sampled_files
    assert "src/index.js" in data.sampled_files


def test_a_single_commit_repository_has_no_pagination_to_read(github):
    github["commits"].respond(json=[COMMITS[0]])
    assert fetch().commit_count == 1


def test_a_transient_server_error_is_retried_once(github):
    github["languages"].mock(side_effect=[httpx.Response(502), httpx.Response(200, json={"Go": 10})])
    sleep = FakeSleep()

    assert fetch(sleep=sleep).languages == {"Go": 10}
    assert SERVER_ERROR_RETRY_DELAY in sleep.delays


def test_a_persistent_server_error_is_reported(github):
    github["languages"].respond(502, text="Bad Gateway")

    with pytest.raises(GitHubError) as exc:
        fetch()
    assert exc.value.status_code == 502


# --- File selection ---------------------------------------------------------

def test_manifests_come_first_shallowest_first_and_are_capped():
    entries = [TreeEntry(f"packages/p{i}/package.json", 100) for i in range(8)]
    entries += [TreeEntry("package.json", 100), TreeEntry("infra/main.tf", 100),
                TreeEntry("Dockerfile", 50), TreeEntry("src/main.py", 5000)]

    assert [e.path for e in select_files(entries)] == [
        "Dockerfile", "package.json", "infra/main.tf",
        "packages/p0/package.json", "packages/p1/package.json", "src/main.py",
    ]


def test_dependencies_build_output_lockfiles_docs_and_binaries_are_never_sampled():
    entries = [TreeEntry(path, 100) for path in [
        "node_modules/x/index.js", "vendor/lib.php", "dist/app.js", "build/main.o",
        "app/dist/x.js", "yarn.lock", "poetry.lock", "assets/logo.png", "docs/guide.md",
        "data.csv", "static/app.min.js", "proto/api_pb2.py", "src/app.py",
    ]]
    assert [e.path for e in select_files(entries)] == ["src/app.py"]


def test_largest_sources_are_taken_within_the_byte_budget():
    entries = [TreeEntry("big.py", MAX_SAMPLED_BYTES + 1)]
    entries += [TreeEntry(f"src/m{i:02d}.py", 9_000) for i in range(20)]

    chosen = select_files(entries)

    assert "big.py" not in [e.path for e in chosen]
    assert len(chosen) == 11
    assert sum(e.size for e in chosen) <= MAX_SAMPLED_BYTES


def test_a_smaller_file_still_fits_after_a_larger_one_is_skipped():
    entries = [TreeEntry("a.py", 60_000), TreeEntry("b.py", 50_000), TreeEntry("c.py", 30_000)]
    assert [e.path for e in select_files(entries)] == ["a.py", "c.py"]


def test_the_file_cap_holds_even_when_bytes_remain():
    entries = [TreeEntry(f"src/m{i:02d}.py", 100) for i in range(40)]
    assert len(select_files(entries)) == MAX_SAMPLED_FILES


def test_empty_files_are_not_worth_a_request():
    assert select_files([TreeEntry("src/__init__.py", 0)]) == []


@pytest.mark.parametrize("path", [
    "Dockerfile.prod", "infra/prod.tf", ".github/workflows/deploy.yaml", "compose.yaml",
    "api/requirements.txt",
])
def test_infrastructure_files_count_as_manifests(path):
    assert is_manifest(path)


@pytest.mark.parametrize("path", ["config.yml", "src/tf.py", "notes/terraform.txt"])
def test_lookalikes_are_not_manifests(path):
    assert not is_manifest(path)


# --- Pagination -------------------------------------------------------------

def test_last_page_is_read_from_the_link_header():
    assert parse_last_page(LINK) == 341


@pytest.mark.parametrize("header", [None, "", '<https://api.github.com/x?page=2>; rel="next"'])
def test_no_last_link_means_no_pagination(header):
    assert parse_last_page(header) is None
