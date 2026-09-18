"""Deterministic authorship signals derived from a repository snapshot.

Pure functions over the data ``github_service`` stored: no network, no model,
the same answer every time. Each signal is a factual observation about how
the repository was developed, with the numbers behind it, so a developer can
check it against the same history and contest it. None of them is a score,
a probability, or a judgement about whether code was written with AI help:

- ``coauthor_trailers``: how many recent commits carry ``Co-authored-by``
  trailers, and which coding agents announce themselves in them.
- ``commit_sizes``: lines changed per commit, week by week, and the weeks that
  stand far above the median.
- ``commit_bursts``: the period the recent commits cover and the busiest hour
  within it.
- ``submitter_share``: the submitter's commits against the whole repository's.
- ``scaffolding``: which generator starter the file layout matches.

Every report carries all five, in this order, so absence of a finding is
stated rather than left to be inferred.
"""

import json
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

SIGNAL_KINDS = (
    "coauthor_trailers", "commit_sizes", "commit_bursts", "submitter_share", "scaffolding",
)
MAX_OBSERVATION_CHARS = 300
MAX_LISTED = 5

BURST_WINDOW = timedelta(hours=1)
# A week stands out when its lines changed per commit reach both this multiple
# of the median week and this many lines.
LARGE_COMMIT_MULTIPLE = 10
LARGE_COMMIT_MIN_LINES = 500


@dataclass
class Signal:
    kind: str
    observation: str
    # Flat on purpose: ints, floats, bools, strings and lists of strings only,
    # so the report schema can hold it and the frontend can render it as is.
    data: dict = field(default_factory=dict)

    def __post_init__(self):
        if len(self.observation) > MAX_OBSERVATION_CHARS:
            self.observation = self.observation[:MAX_OBSERVATION_CHARS - 3].rstrip() + "..."

    def as_dict(self) -> dict:
        return {"kind": self.kind, "observation": self.observation, "data": self.data}


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural if plural is not None else singular + "s")


def derive_authorship_signals(snapshot, *, github_username: str | None = None) -> dict:
    """The ``authorship_signals`` value for a report.

    ``snapshot`` is a ``RepoSnapshot`` row or the ``RepositoryData`` that
    fills one; ``github_username`` is the submitter's GitHub login, or None
    when they have not linked GitHub.
    """
    commits = list(snapshot.commit_history or [])
    stats = snapshot.contributor_stats
    signals = [
        coauthor_trailers(commits),
        commit_sizes(stats),
        commit_bursts(commits, stats, snapshot.commit_count),
        submitter_share(stats, commits, snapshot.commit_count, github_username),
        scaffolding(snapshot.file_tree, snapshot.sampled_files),
    ]
    return {"commits_analyzed": len(commits), "signals": [s.as_dict() for s in signals]}


# --- Co-authored-by trailers -----------------------------------------------

_TRAILER = re.compile(
    r"^co-authored-by:\s*(?P<name>[^<\n]*?)\s*(?:<(?P<email>[^>\n]*)>)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Matched against "name <email>" in lower case. Kept to markers a person is
# unlikely to carry, so a co-author who happens to be called Claude or Devin
# is not mistaken for a tool.
CODING_AGENTS = (
    ("GitHub Copilot", ("copilot",)),
    ("Claude", ("anthropic.com", "claude code")),
    ("Cursor", ("cursor.com", "cursoragent")),
    ("OpenAI Codex", ("openai.com", "codex-connector", "chatgpt-codex")),
    ("Devin", ("devin-ai", "devin.ai", "cognition.ai")),
    ("Aider", ("aider.chat", "aider (")),
    ("Gemini", ("gemini-code-assist", "gemini-cli", "gemini cli")),
    ("Jules", ("google-labs-jules",)),
    ("Windsurf", ("windsurf", "codeium.com")),
    ("OpenHands", ("openhands", "all-hands.dev")),
    ("Sweep", ("sweep.dev", "sweep-ai")),
    ("Amazon Q", ("amazon q", "amazonq")),
    ("Augment", ("augmentcode", "augment code")),
    ("Lovable", ("lovable.dev",)),
    ("Replit Agent", ("replit.com", "replit agent")),
)


def agent_label(name: str, email: str | None) -> str | None:
    """The coding agent a trailer names, or None for anyone else."""
    haystack = f"{name} <{email or ''}>".lower()
    for label, markers in CODING_AGENTS:
        if any(marker in haystack for marker in markers):
            return label
    if name.lower().endswith("[bot]"):
        return name
    return None


def coauthor_trailers(commits: list[dict]) -> Signal:
    coauthored = agent_coauthored = other_coauthored = 0
    agents: set[str] = set()
    for commit in commits:
        trailers = _TRAILER.findall(commit.get("message") or "")
        if not trailers:
            continue
        coauthored += 1
        labels = {agent_label(name, email) for name, email in trailers}
        if labels - {None}:
            agent_coauthored += 1
            agents |= labels - {None}
        if None in labels:
            other_coauthored += 1

    total = len(commits)
    if coauthored == 0:
        observation = f"None of the {total} most recent commits carries a Co-authored-by trailer."
    elif agent_coauthored:
        observation = (f"{agent_coauthored} of the {total} most recent commits"
                       f" {_plural(agent_coauthored, 'names', 'name')} a coding agent as"
                       f" co-author ({', '.join(sorted(agents))})")
        if other_coauthored:
            observation += (f"; {other_coauthored} {_plural(other_coauthored, 'carries', 'carry')}"
                            " a Co-authored-by trailer naming someone else")
        observation += "."
    else:
        observation = (f"{coauthored} of the {total} most recent commits"
                       f" {_plural(coauthored, 'carries', 'carry')} a Co-authored-by trailer;"
                       " none names a recognised coding agent.")
    return Signal("coauthor_trailers", observation, {
        "commits_analyzed": total,
        "coauthored_commits": coauthored,
        "agent_coauthored_commits": agent_coauthored,
        "agents": sorted(agents),
        "other_coauthored_commits": other_coauthored,
    })


# --- Commit sizes -----------------------------------------------------------

def commit_sizes(contributor_stats: list[dict] | None) -> Signal:
    """Lines changed per commit by week, across all contributors.

    GitHub's statistics are weekly totals per author, so this is an average
    per week rather than the size of each commit; the observation says so.
    """
    if contributor_stats is None:
        return Signal("commit_sizes", "Commit sizes are unavailable: GitHub had not finished"
                      " computing contributor statistics when the repository was fetched.",
                      {"available": False})

    lines_by_week: dict[int, int] = defaultdict(int)
    commits_by_week: dict[int, int] = defaultdict(int)
    for contributor in contributor_stats:
        for week in contributor.get("weeks") or []:
            commits_by_week[week["w"]] += week.get("c") or 0
            lines_by_week[week["w"]] += (week.get("a") or 0) + (week.get("d") or 0)
    weeks = sorted(
        (start, lines_by_week[start] / commits, commits, lines_by_week[start])
        for start, commits in commits_by_week.items() if commits > 0
    )
    if not weeks:
        return Signal("commit_sizes", "No commits with line counts appear in the contributor"
                      " statistics.", {"available": True, "weeks_analyzed": 0})

    median = statistics.median(per_commit for _, per_commit, _, _ in weeks)
    threshold = max(LARGE_COMMIT_MULTIPLE * median, LARGE_COMMIT_MIN_LINES)
    outliers = sorted((w for w in weeks if w[1] >= threshold), key=lambda w: -w[1])
    largest = max(weeks, key=lambda w: w[1])

    plural = "s" if len(weeks) != 1 else ""
    observation = (f"Across {len(weeks)} active week{plural} the median commit changed about"
                   f" {median:,.0f} lines")
    if not outliers:
        observation += ", with no week far above that."
    elif len(outliers) == 1:
        observation += f"; the week of {_week_date(outliers[0][0])} stands out at {_describe_week(outliers[0])}."
    else:
        observation += (f"; {len(outliers)} weeks stand out, led by the week of"
                        f" {_week_date(outliers[0][0])} at {_describe_week(outliers[0])}.")
    return Signal("commit_sizes", observation, {
        "available": True,
        "weeks_analyzed": len(weeks),
        "commits_counted": sum(commits for _, _, commits, _ in weeks),
        "median_lines_per_commit": round(median, 1),
        "outlier_threshold_lines_per_commit": round(threshold, 1),
        "largest_week": _week_date(largest[0]),
        "largest_week_lines_per_commit": round(largest[1]),
        "largest_week_commits": largest[2],
        "outlier_weeks": [f"{_week_date(w[0])}: {_describe_week(w)}" for w in outliers[:MAX_LISTED]],
    })


def _describe_week(week) -> str:
    _, per_commit, commits, _ = week
    plural = "s" if commits != 1 else ""
    return f"{per_commit:,.0f} lines per commit across {commits} commit{plural}"


def _week_date(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).date().isoformat()


# --- Commit bursts and velocity --------------------------------------------

def commit_bursts(commits: list[dict], contributor_stats: list[dict] | None,
                  commit_count: int | None) -> Signal:
    """When the recent commits were authored, and the busiest hour among them.

    Merge commits are left out: they are timestamped when a branch was merged,
    not when its work was done. Author dates are used for the same reason.
    """
    merges = sum(1 for c in commits if (c.get("parent_count") or 0) > 1)
    times = sorted(t for t in (_parse_time(c.get("authored_at")) for c in commits
                               if (c.get("parent_count") or 0) <= 1) if t is not None)
    data: dict = {"recent_commits": len(times), "merge_commits": merges,
                  "burst_window_minutes": int(BURST_WINDOW.total_seconds() // 60)}
    if not times:
        return Signal("commit_bursts", "No authored timestamps were available for the recent"
                      " commits.", data)

    burst, burst_start = _largest_burst(times)
    first, last = times[0], times[-1]
    span = last - first
    complete = commit_count is not None and commit_count <= len(commits)
    data.update({
        "first_authored_at": _iso(first),
        "last_authored_at": _iso(last),
        "span_hours": round(span.total_seconds() / 3600, 1),
        "largest_burst_commits": burst,
        "largest_burst_started_at": _iso(burst_start),
        "history_complete": complete,
    })

    n = len(times)
    if n == 1:
        observation = f"A single commit was authored, on {first:%Y-%m-%d}."
    elif burst == n:
        which = f"All {n} commits" if complete else f"The {n} most recent commits"
        observation = f"{which} were authored within {_duration(span)} on {first:%Y-%m-%d}."
    else:
        which = f"All {n} commits" if complete else f"The {n} most recent commits"
        observation = (f"{which} span {_duration(span)} ({first:%Y-%m-%d} to {last:%Y-%m-%d});"
                       f" the busiest hour holds {burst} of them, from {burst_start:%Y-%m-%d %H:%M} UTC.")

    active = _active_weeks(contributor_stats)
    if active:
        active_weeks, span_weeks = active
        data.update({"active_weeks": active_weeks, "span_weeks": span_weeks})
        observation += f" Over the whole history, commits fall in {active_weeks} of {span_weeks} weeks."
    return Signal("commit_bursts", observation, data)


def _largest_burst(times: list[datetime]) -> tuple[int, datetime]:
    best, best_start, start = 1, times[0], 0
    for end in range(len(times)):
        while times[end] - times[start] > BURST_WINDOW:
            start += 1
        if end - start + 1 > best:
            best, best_start = end - start + 1, times[start]
    return best, best_start


def _active_weeks(contributor_stats: list[dict] | None) -> tuple[int, int] | None:
    if not contributor_stats:
        return None
    weeks = {w["w"] for c in contributor_stats for w in c.get("weeks") or [] if w.get("c")}
    if not weeks:
        return None
    span_weeks = (max(weeks) - min(weeks)) // 604800 + 1
    return len(weeks), span_weeks


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _duration(span: timedelta) -> str:
    minutes = int(span.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''}"


# --- Submitter share --------------------------------------------------------

def submitter_share(contributor_stats: list[dict] | None, commits: list[dict],
                    commit_count: int | None, github_username: str | None) -> Signal:
    total = commit_count or (sum(c.get("total") or 0 for c in contributor_stats)
                             if contributor_stats else len(commits))
    if not github_username:
        return Signal("submitter_share", "The submitter has not linked a GitHub account, so"
                      " their share of commits cannot be determined.",
                      {"total_commits": total, "submitter_known": False})

    login = github_username.lower()
    if contributor_stats is None:
        recent = len(commits)
        own = sum(1 for c in commits if (c.get("author_login") or "").lower() == login)
        return Signal("submitter_share", (
            f"{own} of the {recent} most recent commits are by the submitter ({github_username});"
            " contributor statistics for the whole history were not available."
        ), {"submitter": github_username, "submitter_known": True, "statistics_available": False,
            "recent_commits": recent, "submitter_recent_commits": own,
            "share_of_recent": round(own / recent, 3) if recent else 0.0})

    own = sum(c.get("total") or 0 for c in contributor_stats
              if (c.get("login") or "").lower() == login)
    own_added = sum(w.get("a") or 0 for c in contributor_stats
                    if (c.get("login") or "").lower() == login for w in c.get("weeks") or [])
    all_added = sum(w.get("a") or 0 for c in contributor_stats for w in c.get("weeks") or [])
    contributors = len(contributor_stats)
    others = contributors - sum(1 for c in contributor_stats
                                if (c.get("login") or "").lower() == login)
    bots = sorted((c.get("login") or "", c.get("total") or 0) for c in contributor_stats
                  if (c.get("login") or "").endswith("[bot]"))
    share = own / total if total else 0.0

    if own == 0:
        observation = (f"None of the {total:,} commits is by the submitter's GitHub account"
                       f" ({github_username}); {contributors} {_plural(contributors, 'contributor')}"
                       f" {_plural(contributors, 'appears', 'appear')} in the statistics.")
    else:
        observation = f"{own:,} of {total:,} commits are by the submitter ({share:.0%})"
        if others == 0:
            observation += "; no other contributor appears in the statistics."
        else:
            observation += f"; {others} other contributor{'s' if others != 1 else ''} account for the rest"
            if bots:
                observation += ", including " + " and ".join(
                    f"{name} with {count}" for name, count in bots[:2])
            observation += "."
    return Signal("submitter_share", observation, {
        "submitter": github_username,
        "submitter_known": True,
        "statistics_available": True,
        "submitter_commits": own,
        "total_commits": total,
        "share": round(share, 3),
        "contributors": contributors,
        "submitter_additions": own_added,
        "total_additions": all_added,
        "bot_commits": sum(count for _, count in bots),
        "bots": [name for name, _ in bots],
    })


# --- Scaffolding ------------------------------------------------------------

@dataclass(frozen=True)
class Generator:
    name: str
    # Paths a generator writes; "*" matches within one segment, "**/" any depth.
    files: tuple[str, ...]
    # package.json dependencies that identify the tool.
    dependencies: tuple[str, ...] = ()
    # File matches needed on their own, and when a dependency is also present.
    needs: int = 2
    needs_with_dependency: int = 1


GENERATORS = (
    Generator("Create React App",
              ("src/reportWebVitals.js", "src/reportWebVitals.ts", "src/setupTests.js",
               "src/setupTests.ts", "src/App.test.js", "src/App.test.tsx", "src/logo.svg"),
              ("react-scripts",), needs=1, needs_with_dependency=0),
    Generator("Vite",
              ("vite.config.*", "src/vite-env.d.ts", "public/vite.svg", "src/assets/react.svg",
               "src/assets/vue.svg", "src/counter.js", "src/counter.ts"),
              ("vite",)),
    Generator("Next.js", ("next.config.*", "next-env.d.ts"), ("next",)),
    Generator("Angular CLI", ("angular.json",), ("@angular/cli",), needs=1),
    Generator("Vue CLI", ("vue.config.js",), ("@vue/cli-service",), needs=1),
    Generator("Nuxt", ("nuxt.config.*",), ("nuxt",), needs=1),
    Generator("SvelteKit", ("svelte.config.js", "src/app.html"), ("@sveltejs/kit",)),
    Generator("Astro", ("astro.config.*",), ("astro",), needs=1),
    Generator("Gatsby", ("gatsby-config.js", "gatsby-node.js"), ("gatsby",), needs=1),
    Generator("Docusaurus", ("docusaurus.config.*",), ("@docusaurus/core",), needs=1),
    Generator("Remix", ("remix.config.js",), ("@remix-run/dev",), needs=1),
    Generator("NestJS", ("nest-cli.json",), ("@nestjs/core",), needs=1),
    # app.json is too common to count without the dependency.
    Generator("Expo", ("app.json", "app.config.*"), ("expo",), needs=99),
    Generator("React Native CLI", ("metro.config.js", "android/build.gradle", "ios/Podfile"),
              ("react-native",)),
    Generator("Django", ("manage.py", "**/settings.py", "**/wsgi.py", "**/asgi.py"), needs=3),
    Generator("Rails", ("bin/rails", "config/application.rb", "config/routes.rb")),
    Generator("Laravel", ("artisan", "bootstrap/app.php")),
    Generator("Spring Initializr",
              ("mvnw", "gradlew", "src/main/resources/application.properties",
               "src/main/resources/application.yml", "HELP.md")),
    Generator("Flutter", ("pubspec.yaml", "lib/main.dart", ".metadata")),
    Generator(".NET template", ("Program.cs", "appsettings.json", "Properties/launchSettings.json")),
)


def _pattern(spec: str) -> re.Pattern:
    regex = re.escape(spec).replace(r"\*\*/", "(?:.*/)?").replace(r"\*", "[^/]*")
    return re.compile(f"^{regex}$")


_PATTERNS = {spec: _pattern(spec) for g in GENERATORS for spec in g.files}


def package_dependencies(sampled_files: dict | None) -> set[str]:
    """Dependency names from a sampled root package.json, if it parsed."""
    content = (sampled_files or {}).get("package.json")
    if not content:
        return set()
    try:
        manifest = json.loads(content)
    except ValueError:
        return set()
    if not isinstance(manifest, dict):
        return set()
    names: set[str] = set()
    for section in ("dependencies", "devDependencies"):
        if isinstance(manifest.get(section), dict):
            names |= set(manifest[section])
    return names


def scaffolding(file_tree: dict | None, sampled_files: dict | None) -> Signal:
    tree = file_tree or {}
    paths = [entry["path"] for entry in tree.get("entries") or []]
    dependencies = package_dependencies(sampled_files)

    generators: list[str] = []
    matched: list[str] = []
    for generator in GENERATORS:
        hits = [p for spec in generator.files for p in paths if _PATTERNS[spec].match(p)]
        has_dependency = bool(dependencies & set(generator.dependencies))
        needed = generator.needs_with_dependency if has_dependency else generator.needs
        if len(hits) >= needed and (hits or has_dependency):
            generators.append(generator.name)
            matched.extend(sorted(set(hits)))

    if generators:
        names = " and ".join(generators) if len(generators) <= 2 else (
            ", ".join(generators[:-1]) + " and " + generators[-1])
        plural = "s" if len(generators) != 1 else ""
        if matched:
            shown = ", ".join(matched[:4]) + (", ..." if len(matched) > 4 else "")
        else:
            shown = "from package.json dependencies"
        observation = f"The file layout matches the {names} starter{plural} ({shown})."
    else:
        observation = "No generator starter was recognised in the file layout."
    if tree.get("truncated"):
        observation += " GitHub truncated the file tree, so some files were not visible."
    return Signal("scaffolding", observation, {
        "generators": generators,
        "matched_files": matched[:MAX_LISTED * 2],
        "files_in_tree": int(tree.get("entry_count") or len(paths)),
        "tree_truncated": bool(tree.get("truncated")),
    })
