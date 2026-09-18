import os
from datetime import datetime, timezone
from types import SimpleNamespace

# The ORM builds its engine on import; load_dotenv() never overrides a
# variable that is already set.
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from services.authorship_service import (  # noqa: E402
    MAX_OBSERVATION_CHARS, SIGNAL_KINDS, agent_label, coauthor_trailers, commit_bursts,
    commit_sizes, derive_authorship_signals, scaffolding, submitter_share,
)


def commit(authored_at, message="Work", login="octocat", parents=1):
    return {
        "sha": "a" * 40, "message": message, "author_name": "The Octocat",
        "author_login": login, "authored_at": authored_at, "committed_at": authored_at,
        "parent_count": parents, "signature_verified": False,
    }


def week(day, additions, deletions, commits):
    """A GitHub statistics week starting on ``day``, a Sunday."""
    start = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
    return {"w": start, "a": additions, "d": deletions, "c": commits}


def contributor(login, *weeks):
    return {"login": login, "total": sum(w["c"] for w in weeks), "weeks": list(weeks)}


def tree(*paths, truncated=False):
    return {"truncated": truncated, "entry_count": len(paths),
            "entries": [{"path": p, "size": 100} for p in paths]}


STATS = [
    contributor("octocat", week("2026-01-04", 80, 20, 2), week("2026-01-11", 200, 100, 3),
                week("2026-03-01", 14_000, 400, 3)),
    contributor("other", week("2026-01-11", 100, 0, 1)),
    contributor("dependabot[bot]", week("2026-02-01", 30, 30, 3)),
]


# --- Co-authored-by trailers -----------------------------------------------

def test_agent_trailers_are_counted_and_named():
    commits = [
        commit("2026-09-01T10:00:00Z", "Add search\n\nCo-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>"),
        commit("2026-09-01T11:00:00Z", "Fix tests\n\nCo-Authored-By: Claude Opus 4 <noreply@anthropic.com>"),
        commit("2026-09-01T12:00:00Z", "Pair work\n\nCo-authored-by: Sam Lee <sam@example.test>"),
        commit("2026-09-01T13:00:00Z", "Plain commit"),
        commit("2026-09-01T14:00:00Z", "Bot help\n\nCo-authored-by: devin-ai-integration[bot] <x@users.noreply.github.com>"),
    ]

    signal = coauthor_trailers(commits)

    assert signal.observation == (
        "3 of the 5 most recent commits name a coding agent as co-author"
        " (Claude, Devin, GitHub Copilot); 1 carries a Co-authored-by trailer naming someone else."
    )
    assert signal.data == {
        "commits_analyzed": 5, "coauthored_commits": 4, "agent_coauthored_commits": 3,
        "agents": ["Claude", "Devin", "GitHub Copilot"],
        "other_coauthored_commits": 1,
    }


def test_absence_of_trailers_is_stated_as_a_fact():
    signal = coauthor_trailers([commit("2026-09-01T10:00:00Z"), commit("2026-09-01T11:00:00Z")])
    assert signal.observation == "None of the 2 most recent commits carries a Co-authored-by trailer."


def test_only_human_trailers_are_not_reported_as_agents():
    signal = coauthor_trailers([commit("2026-09-01T10:00:00Z", "x\n\nCo-authored-by: Sam Lee <sam@example.test>")])
    assert signal.observation == (
        "1 of the 1 most recent commits carries a Co-authored-by trailer;"
        " none names a recognised coding agent.")
    assert signal.data["agents"] == []


@pytest.mark.parametrize("name, email", [
    ("Claude Dupont", "claude@example.org"),
    ("Devin Smith", "devin@example.org"),
    ("Cursor Fan", "fan@example.org"),
])
def test_people_who_share_a_name_with_a_tool_are_not_tools(name, email):
    assert agent_label(name, email) is None


@pytest.mark.parametrize("name, email, label", [
    ("Claude", "noreply@anthropic.com", "Claude"),
    ("aider (gpt-4o)", "aider@aider.chat", "Aider"),
    ("Cursor Agent", "cursoragent@cursor.com", "Cursor"),
    ("google-labs-jules[bot]", "x@users.noreply.github.com", "Jules"),
    ("some-new-tool[bot]", None, "some-new-tool[bot]"),
])
def test_known_agents_and_bot_accounts_are_recognised(name, email, label):
    assert agent_label(name, email) == label


# --- Commit sizes -----------------------------------------------------------

def test_weekly_commit_sizes_are_combined_across_contributors_with_outliers_named():
    signal = commit_sizes(STATS)

    # Weeks: 2026-01-04 -> 50 lines/commit, 2026-01-11 -> (300+100)/4 = 100,
    # 2026-02-01 -> 20, 2026-03-01 -> 4800. Median 75; threshold max(750, 500).
    assert signal.observation == (
        "Across 4 active weeks the median commit changed about 75 lines;"
        " the week of 2026-03-01 stands out at 4,800 lines per commit across 3 commits.")
    assert signal.data["weeks_analyzed"] == 4
    assert signal.data["commits_counted"] == 12
    assert signal.data["median_lines_per_commit"] == 75.0
    assert signal.data["outlier_threshold_lines_per_commit"] == 750.0
    assert (signal.data["largest_week"], signal.data["largest_week_lines_per_commit"]) == ("2026-03-01", 4800)
    assert signal.data["outlier_weeks"] == ["2026-03-01: 4,800 lines per commit across 3 commits"]


def test_a_history_without_outliers_says_so():
    stats = [contributor("octocat", week("2026-01-04", 80, 20, 2), week("2026-01-11", 120, 30, 3))]
    signal = commit_sizes(stats)
    assert signal.observation == (
        "Across 2 active weeks the median commit changed about 50 lines, with no week far above that.")
    assert signal.data["outlier_weeks"] == []


def test_missing_statistics_are_reported_not_invented():
    signal = commit_sizes(None)
    assert signal.data == {"available": False}
    assert "unavailable" in signal.observation


def test_weeks_without_commits_are_ignored():
    stats = [contributor("octocat", week("2026-01-04", 0, 0, 0))]
    signal = commit_sizes(stats)
    assert signal.data["weeks_analyzed"] == 0


# --- Commit bursts ----------------------------------------------------------

def test_a_whole_project_written_in_under_an_hour_is_described_plainly():
    commits = [commit("2026-09-01T10:00:00Z"), commit("2026-09-01T10:20:00Z"), commit("2026-09-01T10:47:00Z")]

    signal = commit_bursts(commits, None, commit_count=3)

    assert signal.observation == "All 3 commits were authored within 47 minutes on 2026-09-01."
    assert signal.data["largest_burst_commits"] == 3
    assert signal.data["history_complete"] is True
    assert signal.data["span_hours"] == 0.8


def test_the_busiest_hour_is_found_and_merges_are_left_out():
    commits = [
        commit("2026-02-09T09:00:00Z"), commit("2026-02-16T09:00:00Z"),
        commit("2026-02-23T09:00:00Z"), commit("2026-03-02T09:00:00Z"),
        commit("2026-03-08T20:15:00Z"), commit("2026-03-08T20:20:00Z"),
        commit("2026-03-08T20:30:00Z"), commit("2026-03-08T20:45:00Z"),
        commit("2026-03-08T21:00:00Z"), commit("2026-03-08T21:10:00Z"),
        commit("2026-03-08T20:16:00Z", parents=2), commit("2026-03-08T20:17:00Z", parents=2),
    ]

    signal = commit_bursts(commits, STATS, commit_count=341)

    assert signal.observation == (
        "The 10 most recent commits span 27 days (2026-02-09 to 2026-03-08);"
        " the busiest hour holds 6 of them, from 2026-03-08 20:15 UTC."
        " Over the whole history, commits fall in 4 of 9 weeks.")
    assert signal.data["recent_commits"] == 10
    assert signal.data["merge_commits"] == 2
    assert signal.data["largest_burst_started_at"] == "2026-03-08T20:15:00Z"
    assert (signal.data["active_weeks"], signal.data["span_weeks"]) == (4, 9)
    assert signal.data["history_complete"] is False


def test_commits_without_dates_are_skipped():
    signal = commit_bursts([commit(None), commit("not a date"), commit("2026-09-01T10:00:00Z")], None, 3)
    assert signal.observation == "A single commit was authored, on 2026-09-01."
    assert signal.data["recent_commits"] == 1


def test_no_dated_commits_at_all():
    signal = commit_bursts([], None, 0)
    assert signal.data["recent_commits"] == 0
    assert "No authored timestamps" in signal.observation


# --- Submitter share --------------------------------------------------------

def test_share_is_taken_from_statistics_case_insensitively():
    stats = [contributor("octocat", week("2026-01-04", 500, 100, 298)),
             contributor("other", week("2026-01-04", 50, 0, 40)),
             contributor("dependabot[bot]", week("2026-01-04", 30, 30, 3))]

    signal = submitter_share(stats, [], commit_count=341, github_username="OctoCat")

    assert signal.observation == (
        "298 of 341 commits are by the submitter (87%); 2 other contributors account for the rest,"
        " including dependabot[bot] with 3.")
    assert signal.data["share"] == 0.874
    assert (signal.data["submitter_commits"], signal.data["total_commits"]) == (298, 341)
    assert (signal.data["submitter_additions"], signal.data["total_additions"]) == (500, 580)
    assert (signal.data["bot_commits"], signal.data["bots"]) == (3, ["dependabot[bot]"])


def test_a_sole_contributor_has_no_others_to_mention():
    stats = [contributor("octocat", week("2026-01-04", 500, 100, 12))]
    signal = submitter_share(stats, [], commit_count=12, github_username="octocat")
    assert signal.observation == (
        "12 of 12 commits are by the submitter (100%); no other contributor appears in the statistics.")


def test_a_submitter_with_no_commits_is_stated_without_judgement():
    stats = [contributor("someone", week("2026-01-04", 500, 100, 12))]
    signal = submitter_share(stats, [], commit_count=12, github_username="octocat")
    assert signal.observation == (
        "None of the 12 commits is by the submitter's GitHub account (octocat);"
        " 1 contributor appears in the statistics.")
    assert signal.data["submitter_commits"] == 0


def test_without_a_github_account_the_share_is_unknown():
    signal = submitter_share(STATS, [], commit_count=341, github_username=None)
    assert "not linked a GitHub account" in signal.observation
    assert signal.data == {"total_commits": 341, "submitter_known": False}


def test_without_statistics_the_recent_history_is_used_and_labelled_as_such():
    commits = [commit("2026-09-01T10:00:00Z"), commit("2026-09-01T11:00:00Z"),
               commit("2026-09-01T12:00:00Z", login="other"), commit("2026-09-01T13:00:00Z", login=None)]

    signal = submitter_share(None, commits, commit_count=341, github_username="octocat")

    assert signal.observation == (
        "2 of the 4 most recent commits are by the submitter (octocat);"
        " contributor statistics for the whole history were not available.")
    assert signal.data["statistics_available"] is False
    assert signal.data["share_of_recent"] == 0.5


# --- Scaffolding ------------------------------------------------------------

def test_a_vite_starter_is_recognised_from_its_files():
    signal = scaffolding(tree("vite.config.ts", "src/vite-env.d.ts", "public/vite.svg", "src/App.tsx"), {})
    assert signal.observation == (
        "The file layout matches the Vite starter (public/vite.svg, src/vite-env.d.ts, vite.config.ts).")
    assert signal.data == {
        "generators": ["Vite"],
        "matched_files": ["public/vite.svg", "src/vite-env.d.ts", "vite.config.ts"],
        "files_in_tree": 4, "tree_truncated": False,
    }


def test_a_config_file_alone_is_not_a_starter_but_the_dependency_tips_it():
    files = tree("vite.config.ts", "src/main.ts")
    assert scaffolding(files, {}).data["generators"] == []
    manifest = {"package.json": '{"devDependencies": {"vite": "^5.0.0"}}'}
    assert scaffolding(files, manifest).data["generators"] == ["Vite"]


def test_create_react_app_is_recognised_from_the_dependency_alone():
    manifest = {"package.json": '{"dependencies": {"react-scripts": "5.0.1"}}'}
    signal = scaffolding(tree("src/App.js", "src/index.js"), manifest)
    assert signal.data["generators"] == ["Create React App"]
    assert signal.observation == (
        "The file layout matches the Create React App starter (from package.json dependencies).")


def test_django_and_dotnet_starters_are_recognised_from_paths_at_any_depth():
    files = tree("manage.py", "mysite/settings.py", "mysite/wsgi.py", "mysite/urls.py",
                 "Program.cs", "appsettings.json", "Properties/launchSettings.json")
    signal = scaffolding(files, None)
    assert signal.data["generators"] == ["Django", ".NET template"]
    assert signal.observation.startswith("The file layout matches the Django and .NET template starters (")


def test_expo_needs_the_dependency_because_app_json_is_common():
    assert scaffolding(tree("app.json"), {}).data["generators"] == []
    manifest = {"package.json": '{"dependencies": {"expo": "~51.0.0"}}'}
    assert scaffolding(tree("app.json"), manifest).data["generators"] == ["Expo"]


def test_no_starter_and_a_truncated_tree_are_both_stated():
    signal = scaffolding(tree("src/lib.py", "tests/test_lib.py", truncated=True), {})
    assert signal.observation == (
        "No generator starter was recognised in the file layout."
        " GitHub truncated the file tree, so some files were not visible.")
    assert signal.data["tree_truncated"] is True


def test_an_unparsable_package_json_is_ignored():
    signal = scaffolding(tree("package.json", "src/index.js"), {"package.json": "{not json"})
    assert signal.data["generators"] == []


# --- The assembled report value ---------------------------------------------

def snapshot(**overrides):
    fields = {
        "commit_history": [
            commit("2026-09-01T10:00:00Z", "Add search\n\nCo-authored-by: Copilot <copilot@github.com>"),
            commit("2026-09-01T10:30:00Z"),
        ],
        "contributor_stats": STATS,
        "commit_count": 341,
        "file_tree": tree("vite.config.ts", "src/vite-env.d.ts"),
        "sampled_files": {"package.json": '{"devDependencies": {"vite": "5"}}'},
    }
    return SimpleNamespace(**{**fields, **overrides})


def test_every_report_carries_the_five_signals_in_a_fixed_order():
    result = derive_authorship_signals(snapshot(), github_username="octocat")

    assert result["commits_analyzed"] == 2
    assert [s["kind"] for s in result["signals"]] == list(SIGNAL_KINDS)
    assert result["signals"][3]["data"]["submitter_commits"] == 8


def test_signal_data_stays_flat_and_observations_stay_short():
    empty = snapshot(commit_history=[], contributor_stats=None, commit_count=0,
                     file_tree=None, sampled_files=None)
    for github_username in ("octocat", None):
        for snap in (snapshot(), empty):
            for signal in derive_authorship_signals(snap, github_username=github_username)["signals"]:
                assert 0 < len(signal["observation"]) <= MAX_OBSERVATION_CHARS
                for key, value in signal["data"].items():
                    assert isinstance(value, (int, float, bool, str)) or (
                        isinstance(value, list) and all(isinstance(v, str) for v in value)), key


def test_nothing_reads_as_a_verdict_on_ai_use():
    forbidden = ("ai-generated", "ai generated", "suspicious", "likely", "probab", "fraud",
                 "cheat", "score", "risk", "flag")
    result = derive_authorship_signals(snapshot(), github_username="octocat")
    for signal in result["signals"]:
        text = signal["observation"].lower()
        assert not any(word in text for word in forbidden), signal["observation"]
        assert not any("score" in key or "probab" in key for key in signal["data"])


def test_works_on_the_orm_snapshot_row():
    from models.repo_snapshot import RepoSnapshot

    row = RepoSnapshot(project_id="p1", commit_sha="a" * 40, fetch_mode="public",
                       commit_history=snapshot().commit_history, contributor_stats=STATS,
                       commit_count=341, file_tree=tree("manage.py", "app/settings.py", "app/wsgi.py"),
                       sampled_files={})

    result = derive_authorship_signals(row, github_username="octocat")

    assert result["signals"][4]["data"]["generators"] == ["Django"]


def test_the_value_validates_against_the_report_schema():
    reports = pytest.importorskip("schemas.reports")

    result = derive_authorship_signals(snapshot(), github_username="octocat")

    assert set(SIGNAL_KINDS) == set(reports.AUTHORSHIP_SIGNAL_KINDS)
    parsed = reports.AuthorshipSignalsOut.model_validate(result)
    assert [s.kind for s in parsed.signals] == list(SIGNAL_KINDS)
