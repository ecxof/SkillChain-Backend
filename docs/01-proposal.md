# 1. Proposal

SkillChain turns a GitHub repository into an assessment of a developer's claimed skills in
which every verdict cites the lines of code behind it, and every report is published to a
signed, independently timestamped log that anyone can check without trusting SkillChain.

This document covers the problem, the objectives, scope, stakeholders and requirements. The
rest of the set builds on it:

| Document | Covers |
|---|---|
| [02 — Technology stack](02-tech-stack.md) | What the system is built with, and why git and Bitcoin replace a blockchain |
| [03 — Architecture](03-architecture.md) | Components, the analysis pipeline, the attestation design |
| [04 — Database](04-database.md) | Entities, relationships, constraints |
| [05 — UML diagrams](05-diagrams.md) | Use case, activity and class diagrams |
| [06 — Wireframes](06-wireframes.md) | Screens for the React/Vite frontend |
| [07 — API](07-api.md) | Every endpoint's contract |
| [08 — Testing](08-testing.md) | How each requirement below is tested |
| [09 — Verifying a report](09-verifying-a-report.md) | How a stranger checks a report's signature and timestamp |
| [10 — Authorship signals](10-authorship-signals.md) | What each signal means, and what it deliberately does not claim |

---

## 1.1 Problem

A developer's CV claims skills, and a GitHub link is meant to prove them. In practice the
person screening the application usually cannot read the code — for lack of time or of
technical depth — so the claim goes unverified and the link goes unclicked.

Automated screening tools make this worse rather than better. They return a score without
reasons, and the most common complaint about AI-assisted hiring is exactly that: a number
nobody can interrogate. Regulation points the same way. The EU AI Act treats AI used in
recruitment as high-risk and attaches transparency duties to it, Colorado's AI Act covers
automated employment decisions, and Illinois requires employers to explain how AI evaluates
a video interview. None of them asks for immutability; they ask for explanations.

## 1.2 Objectives

Given a repository and a set of claimed skills, produce a verdict for each skill — verified
or not, a level, a confidence — such that:

1. **Every verdict cites evidence.** A verified skill points at a file and a line range in
   the repository, and a verdict whose citations do not resolve to real lines is not
   allowed to stand as verified.
2. **Every report records how it was made.** The exact repository commit, the files the
   model was shown, the model and the prompt version are stored with the verdict.
3. **Every report is independently verifiable.** It is signed into an append-only git log
   and anchored in Bitcoin, so a reader can confirm who issued it and when using stock
   tools, without asking SkillChain anything.
4. **Development patterns are described, not judged.** Facts about how the repository was
   built are reported as facts with the numbers behind them, never as a probability that
   the code was machine-written.

## 1.3 Scope

**In scope**

- Sign-in with Google or GitHub, and linking GitHub to an existing account.
- Submitting a repository with the skills it is claimed to show and the submitter's role.
- Ingesting the repository: metadata, README, file tree, a bounded sample of source files,
  recent commit history and contributor statistics.
- Deterministic authorship signals derived from that history.
- AI analysis producing per-skill verdicts with span-level evidence.
- Persisting each report with the snapshot it was made from.
- Signing, committing, mirroring and timestamping each report.
- Re-running an analysis without overwriting the earlier one.
- A public skill profile aggregated from a developer's public projects.
- A REST API for the React/Vite frontend that follows.

**Out of scope**

- Badges, certificates, NFTs, wallets and per-user on-chain records.
- Human peer review. The analysis is the verdict.
- Statistical detection of AI-written code (see [10](10-authorship-signals.md) for why).
- Job boards, messaging and payments.
- Running, building or executing the analysed code.
- Plagiarism forensics.
- Analysing a private repository without its owner's own GitHub token.

## 1.4 Stakeholders

| Actor | Role | Needs |
|---|---|---|
| Developer | Primary user | Submit repositories, receive an honest assessment, share a profile, contest a signal by pointing at the same history |
| Recruiter or hiring manager | Consumer, unauthenticated | Read a verdict quickly, see the code behind it, verify it without an account |
| System administrator | Operator | Contain model cost, hold the signing key, run the timestamp upgrade job |
| GitHub API | External system | Imposes rate limits (60 requests an hour unauthenticated, 5,000 authenticated) and OAuth scopes |
| LLM provider | External system, xAI by default | Cost, latency and the validity of what it returns |
| OpenTimestamps calendars | External system | Availability, and hours of latency before Bitcoin confirms |

## 1.5 Requirements

Each requirement has an identifier that [08 — Testing](08-testing.md) maps to the tests
that cover it, and a status against the code as it stands:

- **Built** — implemented and tested.
- **Partial** — implemented, with a gap stated in the row.
- **Not built** — not implemented; the reason is given.

### Functional

| ID | Requirement | Status | Where |
|---|---|---|---|
| FR-1 | Sign in with Google | Built | `GET /auth/google/url`, `POST /auth/google/callback` |
| FR-2 | Sign in with GitHub | Built | `GET /auth/github/url`, `POST /auth/github/callback` |
| FR-3 | Link GitHub to an existing account so repositories are fetched with the user's own token | Partial — the OAuth scope is `read:user user:email`, which raises the rate limit but does not grant access to private repositories | `routes/auth.py`, `services/auth_service.py` |
| FR-4 | Unlink GitHub, refused when it is the account's only way in | Built | `DELETE /auth/github/unlink` |
| FR-5 | Submit a project: repository URL, claimed skills, role, visibility | Built | `POST /projects` |
| FR-6 | Report analysis progress through a status the client can poll | Built | `GET /projects/{id}` |
| FR-7 | Ingest metadata, README, file tree, a bounded file sample, commit history and contributor statistics, pinned to one commit | Built | `services/github_service.py` |
| FR-8 | Derive deterministic authorship signals from the history | Built — five signals | `services/authorship_service.py` |
| FR-9 | Produce per-skill verdicts with file-and-line evidence | Built | `services/ai_service.py` |
| FR-10 | Reject citations that do not resolve to real lines, give the model one correction round, and never let a skill left with no resolvable evidence stand as verified | Built | `validate_evidence` in `services/ai_service.py` |
| FR-11 | Persist each report with its snapshot, model and prompt version | Built | `services/analysis_service.py` |
| FR-12 | Sign and commit each report to an append-only log | Built | `services/attestation_service.py` |
| FR-13 | Mirror the log to independent remotes | Built | `ATTESTATION_REMOTES` |
| FR-14 | Timestamp each report in Bitcoin, and complete pending proofs later | Built | `services/timestamp_service.py`, `scripts/upgrade_timestamps.py` |
| FR-15 | Re-run an analysis, appending a new report rather than replacing the old | Built — refused while a run is in flight | `POST /projects/{id}/reanalyze` |
| FR-16 | List, read and delete one's own projects | Built | `GET /projects`, `GET /projects/{id}`, `DELETE /projects/{id}` |
| FR-17 | Read one's own report, whether or not it is public | Built | `GET /projects/{id}/report` |
| FR-18 | Choose a profile address and publish or withdraw the profile | Built | `PATCH /auth/me/profile` |
| FR-19 | Serve an aggregated skill profile to anyone | Built | `GET /public/profiles/{slug}` |
| FR-20 | Serve a public project's report to anyone | Built | `GET /public/reports/{id}` |
| FR-21 | Serve everything needed to verify a report: canonical bytes, hash, commit, Bitcoin proof, commands | Partial — the served `git verify-commit` command omits the signing key, so it fails on a machine that has not configured one (see [09](09-verifying-a-report.md#a-known-gap)) | `GET /public/reports/{id}/attestation` |
| FR-22 | Publish the signing key where a verifier can find it from the domain alone | Built | `GET /.well-known/skillchain-signing-key` |

### Non-functional

| ID | Requirement | Status | Notes |
|---|---|---|---|
| NFR-1 | An analysis completes within 60 seconds for a typical repository | Not measured | No timing benchmark exists yet. `duration_ms` is recorded on every report, so it can be measured from real runs. |
| NFR-2 | A verdict stands as verified only if its evidence resolves to real lines | Built | Enforced when the report is stored, not merely requested of the model |
| NFR-3 | Reports are reproducible from what was stored | Partial | Every input is stored — the commit, the exact file contents, the model and the prompt version. The model's output is not guaranteed identical across runs; re-running reproduces the question, not necessarily the answer. |
| NFR-4 | Every published report is verifiable with stock `git` and OpenTimestamps tooling | Partial | Verifies with `git` once the published key is supplied; see FR-21. The `ots` command-line client does not run on Windows. |
| NFR-5 | No secret is committed to the repository | Built | Configuration comes from `.env`; `.env.example` documents it |
| NFR-6 | The signing key never leaves the server | Built | Only the public half is served |
| NFR-7 | A per-user limit on analyses, to cap model cost | Not built | The value is an open decision |
| NFR-8 | Ingestion is bounded to cap cost | Built | At most 15 sampled files and 100 KB of sampled content |
| NFR-9 | Degrade gracefully when GitHub, the model, git or the calendars fail | Built | An analysis that has been paid for is never lost to a publishing failure |
| NFR-10 | A resource a caller may not see is indistinguishable from one that does not exist | Built | Unowned and unpublished resources answer 404, never 403 |

## 1.6 Limitations

These are stated plainly because a reader deciding whether to trust a report needs them.

- **The model can be wrong.** Span validation guarantees that cited lines exist; it cannot
  guarantee that they show what the model says they show. That is why the lines are shown
  to the reader rather than summarised away.
- **A repository evidences exposure to a skill, not sole authorship of the code.**
- **An analysis describes one commit at one moment.** A later push is not reflected until
  the project is re-analysed.
- **Authorship signals describe development patterns.** They are not evidence that AI was
  used, and are never presented as a score.
- **Unauthenticated fetching is limited to 60 GitHub requests an hour per IP address.**
  Linking GitHub raises it to 5,000.
- **Private repositories cannot be analysed yet**, because the GitHub OAuth scope does not
  cover them (FR-3).
- **Bitcoin anchoring completes hours after a report is issued**, not instantly. Until then
  the report carries a pending proof, and its signature is what establishes it.
- **Only a sample of the repository is read** — up to 15 files — so a skill evidenced only
  in files outside the sample can go unrecognised.

## 1.7 Decisions that shaped the design

The system began as something different: human peer review of submissions, badges minted
as soulbound NFTs on a blockchain testnet, and a MetaMask wallet on every account. Each of
those was removed for a reason, and the reasons are what the current design rests on.

- **No human review.** A reviewer adds latency, cost and an inconsistency of their own, and
  the thing a recruiter lacks is not a second opinion but the ability to see the evidence.
  Span-level citations give them that directly.
- **No badges, wallets or per-user on-chain records.** The trust gap in AI-assisted hiring
  is explainability, not tamper-proofing. A soulbound token binds a sensitive claim
  permanently to a wallet, is hard to revoke, and asks the recruiter — the person verifying
  — to use chain tooling. The testnet it was minted on offered no permanence either.
- **Integrity from git and Bitcoin instead.** The one thing the blockchain was genuinely
  doing — proving a record existed unaltered at a point in time — is kept and done better.
  See [02](02-tech-stack.md#why-git-and-bitcoin-instead-of-a-blockchain).
- **Evidence at the level of lines, not files.** A citation of a whole file is not a
  citation; the reader has to be able to open the exact lines.
- **No AI-code detector.** The technique is unreliable on prose and worse on code, and its
  errors fall hardest on the developers this product should serve best. See
  [10](10-authorship-signals.md#why-there-is-no-ai-code-detector).
