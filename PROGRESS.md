# SkillChain — Project Log

**Last updated:** 17 September 2026

This file records what SkillChain is, how the design arrived at its current shape, what
has actually been built, and what happens next. It is the document to read first when
returning to this project after a break.

---

## Status at a glance

| | |
|---|---|
| **Phase** | Backend foundations complete; the analysis pipeline itself is not yet built |
| **Branch** | `feature/api-request-response-schemas` (2 commits ahead of `main`, unmerged) |
| **Merged so far** | 4 pull requests, 18 commits |
| **Tests** | 68 passing (`pytest`, 0.22 s) |
| **Runtime** | Python 3.14.6 · FastAPI 0.136 · SQLAlchemy 2.0 · PostgreSQL 18 (Docker) · git 2.49 |
| **Working endpoints** | Google + GitHub OAuth, `/auth/me`, health check |
| **Not yet built** | GitHub ingestion, authorship signals, AI analysis, attestation, timestamping, public profiles |

---

## 1. What SkillChain is

A developer submits a GitHub repository together with the skills they claim it
demonstrates. SkillChain reads the repository, asks an LLM to assess each claimed skill,
and produces a report in which **every verdict cites a specific file and line range**. The
report is then signed and published to an append-only log so a third party can verify it
independently.

**The problem.** A CV claims skills; a GitHub link supposedly proves them. The recruiter
screening the application cannot read the code — from lack of time or lack of technical
depth — so the claim goes unverified and the link goes unclicked. Existing AI screening
tools make this worse by returning an unexplained score.

**The objective.** Turn a repository into an evidence-backed skill assessment that is
readable in seconds, checkable line by line, reproducible, and verifiable without trusting
SkillChain's servers.

---

## 2. How the design got here

The project began as an AI-verified skills platform that minted **soulbound NFT badges on
Monad testnet**, gated behind MetaMask wallet signing, with human peer review before
issuance. Four rounds of questions reshaped it. Each decision and its reasoning:

### 2.1 Human peer review — removed

The AI verdict is final. The `Review` model and `/reviews` router are gone. This removes
an entire reviewer-permissions subsystem that the `User` model had no fields to support.

### 2.2 Badges — removed entirely

There is no badge, credential, certificate or NFT object in the system. The output is the
**report** plus an aggregated **skill profile**.

### 2.3 Blockchain and MetaMask — replaced, not merely deleted

The chain was doing two separable jobs. Separating them is what unlocked the redesign:

- **Identity/ownership** — "this badge belongs to `0xabc`". The only reason MetaMask
  existed. **Dropped outright.**
- **Integrity/timestamping** — "this record existed at time T, unaltered". **Kept, and
  done better.**

Reasons the original approach was abandoned:

- The trust gap in AI-assisted hiring is **explainability, not tamper-proofing**. The
  dominant recruiter complaint is a high score with no reasoning. Blockchain answers a
  question nobody was asking.
- Regulation agrees. The EU AI Act (enforcement Aug 2026), Colorado SB 205 and Illinois
  AIVIA require AI hiring tools to be auditable and explainable. None require immutability.
- Soulbound tokens are documented as misaligned with employment use cases — permanent
  public binding of sensitive claims to a wallet, weak revocation, no cross-chain standard.
- **Monad testnet is not durable storage.** Testnets get reset. A credential meant to be
  permanent sat on infrastructure with no permanence guarantee.
- The wallet blocked the wrong person. A recruiter would have needed chain tooling to
  verify a badge — the one participant who must face zero friction.
- GitRoll, the closest competitor, uses no blockchain, badges or certificates at all.

**What replaced it, in two independent layers:**

- **Authorship — git.** Git is the structure blockchains reinvented: a content-addressed
  append-only Merkle DAG where a commit SHA hashes both tree and parent, so any rewrite
  changes every subsequent SHA. Since git 2.34 commits sign with a plain SSH key, and the
  signature lives *inside the commit object* — so verification needs only the commit and
  the public key. No host, GitHub included, has authority over it; remotes are mirrors.
  Pushing to several independent remotes means no single host can erase the log.
- **Time — Bitcoin, via OpenTimestamps.** Free public calendar servers aggregate thousands
  of hashes into a Merkle tree and anchor one root into Bitcoin via `OP_RETURN`. No wallet,
  no gas, no contract, no RPC, no API key — only a SHA-256 hash leaves the machine.

The signature answers *who said it*; the anchor answers *when*, independently of SkillChain
and of every git host. This is the same pattern the software supply-chain world chose over
blockchain — Sigstore's Rekor is an append-only Merkle transparency log with signed
checkpoints, and SLSA/in-toto provenance runs on it.

### 2.4 Evidence is span-level

A verdict must cite `{file, start_line, end_line}`, not a vague claim. Every span is
validated against the stored snapshot before the report is accepted — a citation pointing
at a nonexistent file or an out-of-range line is rejected rather than shipped.

### 2.5 No AI-code detector

Tempting, since AI-written code undermines the premise, but the technique fails this use
case. AI-text detectors test below 80% accuracy, with 2–15% false positives for native
English speakers and **61% for non-native speakers**. Code is worse: the core signal is low
perplexity, and good code is predictable *by design* — boilerplate, scaffolding and
idiomatic patterns would all read as fraudulent, disproportionately penalising the exact
cohort this product should serve best. Under the EU AI Act this is high-risk hiring use.

Instead SkillChain derives **deterministic authorship signals from git history**:
`Co-authored-by: Copilot` trailers, commit size distribution, development velocity, the
submitter's commit share, scaffold detection. These are checkable facts a developer can
contest by pointing at the same history — never presented as an "AI score".

### 2.6 GitHub access — both modes

Authenticated via the user's stored token (5000 req/hr, private repos) when linked;
unauthenticated public-only (60 req/hr) otherwise.

### 2.7 AI provider — provider-agnostic, xAI default

`grok-3` via the OpenAI-compatible SDK stays the default, but behind an `LLMProvider`
protocol with the model name and prompt version in config — required anyway for the
reproducibility model.

---

## 3. Work completed

### PR #1 — `chore/dependency-pins-and-pyjwt`

The original `requirements.txt` was a PowerShell `pip freeze` dump saved as **UTF-16**, so
`pip install -r` could not parse it, and it listed ~70 transitive packages including the
entire Ethereum tree.

- **Pinned all dependencies to verified exact versions** — rewritten as direct dependencies
  only, each checked against PyPI for existence, non-yanked status, and a Python 3.14
  Windows x64 wheel so installation needs no compiler.
- **Replaced python-jose with PyJWT.** python-jose is effectively unmaintained; PyJWT is
  the actively maintained standard.
- **Depended on the `opentimestamps` library rather than `opentimestamps-client`.** The
  client's `ots` CLI imports python-bitcoinlib's OpenSSL bindings, which fail to load on
  Windows. Stamping and upgrading only need the library.

### PR #2 — `chore/remove-wallet-badges-reviews`

- **Removed wallet linking** — both wallet endpoints, the `web3`/`eth_account` signature
  verification, and `User.wallet_address`.
- **Removed badges and peer reviews** — `models/badge.py`, `models/review.py`,
  `routes/badges.py`, `routes/reviews.py`, `services/badge_service.py`, and their router
  registrations.
- **Switched token expiry to timezone-aware UTC** — `datetime.utcnow()` is deprecated and
  returns a naive datetime, which is a genuine correctness trap around expiry comparisons.

### PR #3 — `chore/set-up-alembic-migrations`

- **Development database in Docker** — `docker-compose.yml` running `postgres:18` with the
  named volume `skillchain_pgdata` mounted at `/var/lib/postgresql` (the 18+ path, not the
  older `/var/lib/postgresql/data`), so data survives container rebuilds.
- **Explicit constraint naming convention** on `Base.metadata` — without it Alembic
  generates migrations that cannot drop unnamed constraints later.
- **Alembic migrations replace `create_all`.** `Base.metadata.create_all()` silently
  ignores changes to existing tables, which would have become a real problem the moment the
  schema evolved. The baseline migration captures users and projects.

### PR #4 — `feature/analysis-report-data-model`

- **The analysis pipeline data model** — three new tables plus a reshaped `Project`:
  - `RepoSnapshot` — exactly what was fetched and how (commit SHA, languages, topics, star
    and fork counts, commit count, README, file tree, sampled file contents, commit history,
    contributor stats, `fetch_mode`). **This is what makes a report reproducible.**
  - `AnalysisReport` — model name, prompt version, trust score, summary, authorship signals,
    raw response, timing and token usage, plus attestation fields (`content_hash`,
    `attestation_commit_sha`, `attestation_status`, `timestamp_status`, `anchored_at`,
    `bitcoin_block_height`).
  - `SkillAssessment` — per-skill verdict with the span-level `evidence` array.
- **GitHub username recorded at login**, needed later to compute the submitter's own commit
  share against contributor stats.

### Current branch — `feature/api-request-response-schemas` (not yet merged)

- **Request and response schemas** — `schemas/auth.py`, `projects.py`, `reports.py`,
  `profiles.py` replace the empty `schemas/validators.py`, including GitHub URL validation
  that parses owner/repo, and the `EvidenceSpan` shape. 197 lines of schema tests.
- **Typed auth routes** — `routes/auth.py` now declares `response_model` on every endpoint,
  with its inline Pydantic classes moved into `schemas/auth.py`, plus `pytest.ini`, a test
  `conftest.py` and 68 tests covering the auth routes and schemas.

### Also completed outside the PR sequence

- **The hardcoded Google OAuth credentials are out of source.** `routes/auth.py` previously
  contained the live client ID *and client secret* as string literals; both now read from
  the environment, alongside `FRONTEND_URL`.
- `.env.example` documents every variable, including the attestation and timestamping
  settings, both defaulting to disabled so local development needs no signing key.

> ⚠️ **Outstanding manual action:** the Google client secret that was committed in source
> still needs to be **rotated in Google Cloud Console**. Removing it from the code does not
> invalidate it.

---

## 4. Current state of the codebase

| Path | State |
|---|---|
| `main.py` | App, CORS, three routers registered |
| `db/database.py` | Engine, session, `get_db`, constraint naming convention |
| `models/` | `user`, `project`, `repo_snapshot`, `analysis_report`, `skill_assessment` — complete |
| `migrations/` | Alembic configured; baseline + pipeline migrations |
| `schemas/` | `auth`, `projects`, `reports`, `profiles` — complete |
| `routes/auth.py` | **Complete** — Google + GitHub OAuth, `/auth/me`, fully typed |
| `routes/projects.py` | Stub — returns a placeholder message |
| `routes/ai.py` | Stub — returns a placeholder message |
| `services/auth_service.py` | **Complete** — JWT, `get_current_user`, get-or-create with account linking |
| `services/ai_service.py` | Written but **unreachable** — no route calls it; needs the Phase F rewrite |
| `services/github_service.py` | **Empty file** — next task |
| `tests/` | `conftest.py`, `test_auth_routes.py`, `test_schemas.py` — 68 tests |
| `docs/` | **Does not exist yet** — Phase K |

---

## 5. Roadmap

| Phase | Work | Status |
|---|---|---|
| A | Environment, repo, dependencies, secrets, Alembic | ✅ Done |
| B | Strip blockchain, badges, reviews | ✅ Done |
| C | Pydantic schemas | ✅ Done (unmerged) |
| — | Analysis data model | ✅ Done |
| **D** | **`github_service.py` — repo ingestion** | ⬅ **Next** |
| E | `authorship_service.py` — deterministic signals | Pending |
| F | `ai_service.py` rewrite — provider interface, span evidence | Pending |
| G | `attestation_service.py` + `timestamp_service.py` | Pending |
| H | `analysis_service.py` + `profile_service.py` | Pending |
| I | Routes — projects, reports, public | Pending |
| J | Full test suite across the pipeline | Pending |
| K | `docs/` — proposal, architecture, UML diagrams, wireframes | Pending |

---

## 6. What happens next, and how

### Phase D — `services/github_service.py`

An async httpx client that uses `User.github_access_token` when the user has linked GitHub,
and falls back to unauthenticated requests otherwise.

**Calls to make:**

| Endpoint | For |
|---|---|
| `GET /repos/{owner}/{repo}` | description, stars, forks, topics, default branch, private flag |
| `GET /repos/{owner}/{repo}/languages` | language byte counts |
| `GET /repos/{owner}/{repo}/commits?per_page=1` | total commit count, read from the `Link` header's `rel="last"` page number |
| `GET /repos/{owner}/{repo}/commits?per_page=100` | commit metadata and messages for authorship signals |
| `GET /repos/{owner}/{repo}/stats/contributors` | per-author weekly commit/addition/deletion counts in one call |
| `GET /repos/{owner}/{repo}/readme` | README (base64) |
| `GET /repos/{owner}/{repo}/git/trees/{sha}?recursive=1` | full file tree |
| `GET /repos/{owner}/{repo}/contents/{path}` | contents of selected files |

**Gotchas to handle:**

- `stats/contributors` returns **`202 Accepted` with an empty body** while GitHub computes
  the statistics. Retry with backoff; treat persistent 202 as "stats unavailable" rather
  than failing the run.
- The tree response can be `truncated: true` on very large repos — detect and degrade.
- Rate limits differ by 83× between modes. Read `X-RateLimit-Remaining` and raise a typed
  error rather than letting a 403 surface as a generic failure.

**File-sampling heuristic:** always take manifests (`package.json`, `requirements.txt`,
`pyproject.toml`, `go.mod`, `Cargo.toml`, `pom.xml`, `composer.json`, `Dockerfile`,
`docker-compose.yml`, `*.tf`), then the largest source files by extension allowlist. Skip
`node_modules/`, `vendor/`, `dist/`, `build/`, `.min.`, lockfiles and binaries. Cap at
**15 files / 100 KB**.

**Critical detail:** retain the full text of every sampled file in the snapshot. Evidence
spans are line ranges, and they must stay renderable later without re-fetching from GitHub —
the repo may have changed or been deleted.

**Typed errors** for: repo not found (404), rate limited (403 with the reset time), private
repo without a token, and truncated tree.

### Phase E — `services/authorship_service.py`

Pure functions over a stored snapshot. No LLM, no network, fully unit-testable against
fixture histories:

- `Co-authored-by:` trailer detection — Copilot and other coding agents announce themselves.
- Commit size distribution — median and outliers.
- Commit burst and velocity detection — an entire project in three commits in one hour.
- The submitter's commit share, from contributor stats matched on `github_username`.
- Scaffold detection from the file tree (`create-react-app`, `vite`, framework generators).

Output is neutral and factual, with the numbers attached. **No score, no AI-probability, no
accusatory phrasing.**

### Phase F — `services/ai_service.py` rewrite

An `LLMProvider` protocol with `XAIProvider` as the default, reading `XAI_API_KEY` and
`AI_MODEL`. Prompts move to `services/prompts.py` behind an explicit `PROMPT_VERSION`
constant, which is stored on every report.

The prompt gains the file tree and **line-numbered** file contents, so the model can cite
spans. The response schema requires each skill to carry `evidence` as
`{file, start_line, end_line, detail}`.

**Every span is validated against the stored snapshot.** A nonexistent file or an
out-of-range line means reject and retry once; on a second failure the skill is marked
unevidenced rather than shipping a hallucinated citation. This validation is what makes the
explainability claim real rather than decorative.

### Then

**G** — canonical JSON, content hashing, locked append-only signed commits, multi-remote
mirroring, OpenTimestamps stamping, and `scripts/upgrade_timestamps.py` to upgrade pending
proofs once Bitcoin confirms. **H** — pipeline orchestration and profile aggregation.
**I** — the project, report and public routes. **J** — pipeline tests. **K** — the `docs/`
set, including the use case, activity and class diagrams.

---

## 7. Open decisions

- **`User.github_access_token` is stored in plaintext.** Encrypt at rest with a Fernet key,
  store only short-lived tokens, or accept and document it.
- **Signing-key custody and rotation.** Rotation needs `allowed_signers` to retain old keys
  with validity windows, or historical signatures stop verifying.
- **Which remotes to mirror to**, and whether the log publishes every report or only those
  whose project is public. Likely public projects only, so `profile_is_public` stays
  meaningful.
- **Cadence of `upgrade_timestamps.py`.** Bitcoin confirmation takes hours, so hourly is ample.
- **Per-user analysis rate limit** for LLM cost control — value undecided.
- **Hosting.** The app takes a single `DATABASE_URL`, so the move is configuration only. To
  decide then: API host, managed Postgres (must offer 18), TLS, Alembic as a deploy step,
  backups, and where the signing key and attestation log live — hosted platforms often have
  ephemeral filesystems.

---

## 8. Running it locally

Start the database:

```bash
docker compose up -d
```

Apply migrations:

```bash
./venv/Scripts/python.exe -m alembic upgrade head
```

Run the API:

```bash
./venv/Scripts/python.exe -m uvicorn main:app --reload
```

Run the tests:

```bash
./venv/Scripts/python.exe -m pytest
```

Interactive API documentation is at <http://localhost:8000/docs>.
