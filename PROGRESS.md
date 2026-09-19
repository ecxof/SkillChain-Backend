# SkillChain — Project Log

**Last updated:** 20 September 2026

This file records what SkillChain is, how the design arrived at its current shape, what
has actually been built, and what happens next. It is the document to read first when
returning to this project after a break.

---

## Status at a glance

| | |
|---|---|
| **Phase** | Ingestion, authorship signals and AI analysis built; attestation not yet started |
| **Branch** | `feature/span-evidence-ai-analysis` (unmerged) |
| **Merged so far** | 6 pull requests, 23 commits |
| **Tests** | 202 passing (`pytest`, 0.9 s) |
| **Runtime** | Python 3.14.6 · FastAPI 0.136 · SQLAlchemy 2.0 · PostgreSQL 18 (Docker) · git 2.49 |
| **Working endpoints** | Google + GitHub OAuth, `/auth/me`, health check |
| **Not yet built** | Attestation, timestamping, pipeline orchestration, project and public routes |

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

Authenticated via the user's stored token (5000 req/hr) when linked; unauthenticated
public-only (60 req/hr) otherwise. **Note:** the OAuth scope currently requested is
`read:user user:email`, which does not grant repository contents, so private-repository
analysis is not yet actually available — see §7.

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

### PR #5 — `feature/api-request-response-schemas`

- **Request and response schemas** — `schemas/auth.py`, `projects.py`, `reports.py`,
  `profiles.py` replace the empty `schemas/validators.py`, including GitHub URL validation
  that parses owner/repo, and the `EvidenceSpan` shape.
- **Typed auth routes** — `routes/auth.py` declares `response_model` on every endpoint,
  with its inline Pydantic classes moved into `schemas/auth.py`, plus `pytest.ini`, a test
  `conftest.py` and 68 tests covering the auth routes and schemas.

### PR #6 — `feature/github-ingestion-authorship-signals`

- **`services/github_service.py` — repository ingestion.** One call fills every
  `RepoSnapshot` column: metadata, languages, commit count from the `Link` header, recent
  commit history, contributor statistics, README, file tree and a bounded sample of source
  files, **all pinned to the head commit** so a run is reproducible and evidence line ranges
  keep resolving. Manifests are sampled first, then the largest source files, skipping
  dependencies, build output, lockfiles and generated code, within 15 files and 100 KB.
  Contributor statistics are requested early and polled through their `202` warm-up; a
  truncated tree is flagged rather than fatal. Typed errors for missing repositories,
  exhausted rate limits, rejected tokens and empty repositories.
- **`services/authorship_service.py` — deterministic signals.** Pure functions over a
  stored snapshot, so the same snapshot always yields the same signals. Every report carries
  the same five: `Co-authored-by` trailers and the coding agents they name, lines changed per
  commit by week with the weeks that stand out, the period the recent commits cover and the
  busiest hour within it, the submitter's commit share, and the generator starter the file
  layout matches. A test asserts no observation reads as a verdict on AI use.

### Current branch — `feature/span-evidence-ai-analysis` (not yet merged)

- **`services/ai_service.py` rewritten behind an `LLMProvider` protocol**, with
  `XAIProvider` as the default reading `XAI_API_KEY` and `AI_MODEL`. The client is built on
  first use rather than at import, so the module loads without a key.
- **`services/prompts.py`** holds the prompt behind `PROMPT_VERSION`, stored on every
  report beside the model name. The README and sampled files are presented **line-numbered**,
  and repository content is framed as material to assess rather than instructions to follow.
- **Citations are verified, not trusted.** Every span is checked against the snapshot: the
  file must be one the model was shown and the lines must exist in it. Failures are dropped
  and one retry is spent telling the model what did not resolve; a skill with nothing left is
  reported **unverified** with the reason saying so. The authorship signals are deliberately
  kept out of the prompt, so a development pattern cannot colour a skill verdict.

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
| `services/github_service.py` | **Complete** — ingestion, sampling, typed errors |
| `services/authorship_service.py` | **Complete** — the five deterministic signals |
| `services/ai_service.py` | **Complete** — provider protocol, span validation, retry |
| `services/prompts.py` | **Complete** — versioned prompt, line-numbered files |
| `services/attestation_service.py` | **Does not exist yet** — next task |
| `tests/` | 202 tests across auth, schemas, ingestion, authorship and analysis |
| `docs/` | **Does not exist yet** — Phase K |

---

## 5. Roadmap

| Phase | Work | Status |
|---|---|---|
| A | Environment, repo, dependencies, secrets, Alembic | ✅ Done |
| B | Strip blockchain, badges, reviews | ✅ Done |
| C | Pydantic schemas | ✅ Done |
| — | Analysis data model | ✅ Done |
| D | `github_service.py` — repo ingestion | ✅ Done |
| E | `authorship_service.py` — deterministic signals | ✅ Done |
| F | `ai_service.py` rewrite — provider interface, span evidence | ✅ Done (unmerged) |
| **G** | **`attestation_service.py` + `timestamp_service.py`** | ⬅ **Next** |
| H | `analysis_service.py` + `profile_service.py` | Pending |
| I | Routes — projects, reports, public | Pending |
| J | Full test suite across the pipeline | Pending |
| K | `docs/` — proposal, architecture, UML diagrams, wireframes | Pending |

---

## 6. What happens next, and how

### Phase G — `attestation_service.py` and `timestamp_service.py`

The two layers that let a third party verify a report without trusting SkillChain. Both sit
behind `ATTESTATION_ENABLED` and `OTS_ENABLED`, which default to false, so local development
and tests need no signing key and reach no network.

**Canonical serialisation first.** `json.dumps(report, sort_keys=True, ensure_ascii=False,
separators=(",", ":"))` encoded UTF-8 with a trailing LF. Re-serialising the same report must
give identical bytes, because `content_hash = sha256(canonical_json)` is the report's
address, and the repository's `.gitattributes` already forces LF so a Windows checkout cannot
change the hash.

**The log.** Content-addressed, git-style sharded: `reports/<hash[:2]>/<hash>.json`. One
signed commit per report, `git -c gpg.format=ssh -c user.signingkey=<key> commit -S`, whose
SHA becomes the report's permanent public ID. Commits are serialised behind an application
lock — single writer, append-only, never rebased, and a re-analysis appends rather than
amends. `ATTESTATION_REMOTES` is pushed to best-effort, never blocking, so no single host can
erase the log. The public key is served at `/.well-known/skillchain-signing-key` in
`allowed_signers` format so anyone can run `git verify-commit` without contacting support.

**The timestamp.** The `opentimestamps` library submits the digest to public calendars via
`RemoteCalendar` and writes a pending `.ots` proof immediately;
`scripts/upgrade_timestamps.py` upgrades pending proofs once Bitcoin confirms, hours later,
and commits the upgraded proofs in follow-up commits, which the append-only model
accommodates naturally. Anchor checks compare the proof's Merkle root against a block header
from a public source, since `bitcoin.rpc` does not load on Windows.

**Nothing here may lose an analysis.** A git, network or calendar failure sets
`attestation_status` or `timestamp_status` to `failed` and the report still saves and serves.

### Phase H — `analysis_service.py` and `profile_service.py`

`run_pipeline(project_id)` orchestrates fetch → snapshot → authorship signals → analyse →
persist → attest → complete, updating `Project.status` at each step and recording failures in
`error_message` rather than raising into the submitting request. `POST /projects` returns 202
and schedules it; the frontend polls. `profile_service` aggregates a user's completed reports
into the public skill profile, honouring `profile_is_public`.

### Then

**I** — the project, report and public routes per §7 of the proposal, reusing the existing
`get_current_user` dependency. **J** — end-to-end pipeline tests with a mocked GitHub and a
stubbed provider, driving a real analysis into a temporary git repo and asserting the commit
exists, is signed, and that the committed bytes hash to `content_hash`. **K** — the `docs/`
set, including the use case, activity and class diagrams.

---

## 7. Open decisions

- **The GitHub OAuth scope does not cover private repositories.** `routes/auth.py` requests
  `read:user user:email`, so a linked token today buys only the higher rate limit. Either add
  the `repo` scope (a broad grant the user must accept), register a GitHub App with read-only
  contents permission (the proper route, more work), or state plainly that private-repository
  analysis is deferred.
- **Two auth tests depend on a local `.env`.** They pass only because a `SECRET_KEY` is
  present; on a clean checkout they fail. `tests/conftest.py` should set a default key of at
  least 32 bytes next to the `DATABASE_URL` default.
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
