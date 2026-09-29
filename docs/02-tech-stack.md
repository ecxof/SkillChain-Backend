# 2. Technology stack

## 2.1 The stack

Versions are the exact pins in `requirements.txt`. Each was checked to exist on PyPI, not to be
yanked, and to ship a wheel for Python 3.14 on Windows x64, so installing needs no compiler.

| Layer | Choice | Version |
|---|---|---|
| Language | Python | 3.14 |
| Web framework | FastAPI, served by Uvicorn | 0.136.1, 0.47.0 |
| Validation | Pydantic | 2.13.5 |
| ORM | SQLAlchemy | 2.0.52 |
| Database | PostgreSQL, in Docker locally | 18 |
| Database driver | psycopg2 | 2.9.13 |
| Migrations | Alembic | 1.20.0 |
| Configuration | python-dotenv | 1.2.3 |
| Sign-in | OAuth 2.0 with Google and GitHub | — |
| Session tokens | JWT, HS256, via PyJWT | 2.14.0 |
| HTTP client | httpx, async | 0.28.1 |
| Language model | xAI `grok-3`, through the OpenAI SDK | 3.13.0 |
| Attestation log | git, with SSH-signed commits | 2.34 or later |
| Timestamping | The OpenTimestamps library | 0.4.5 |
| Background work | FastAPI `BackgroundTasks` | — |
| Scheduled work | `scripts/upgrade_timestamps.py`, run by cron or Task Scheduler | — |
| Tests | pytest, FastAPI's `TestClient`, respx | 9.1.1, —, 0.23.1 |
| Frontend (next) | React with Vite | — |

## 2.2 Choices worth explaining

**FastAPI.** Almost all of an analysis is waiting — on GitHub, on the model, on git, on the
timestamp calendars — so an async framework keeps one process responsive while it waits. The
Pydantic models that validate each request are also the contract the frontend is written
against, and FastAPI publishes them as interactive documentation at `/docs` without a separate
specification to keep in step.

**PostgreSQL, with SQLite under test.** PostgreSQL enforces the check constraints and cascades
the schema depends on. The tests run the same models against in-memory SQLite, with foreign
keys switched on so the cascades behave as they do in production. The application reads a
single `DATABASE_URL`, so moving to a hosted database is configuration, not code.

**PyJWT rather than python-jose.** python-jose is effectively unmaintained; PyJWT is the
maintained standard.

**The model sits behind an interface.** `LLMProvider` is a protocol with one method, `complete`.
The xAI implementation uses the OpenAI SDK because xAI serves an OpenAI-compatible API, but
nothing outside `ai_service` knows which provider is in use, and every report records the
model that produced it. Changing provider is a new class, not a change to the pipeline.

**`BackgroundTasks` rather than a job queue.** An analysis runs in the API process after the
response is sent, and `projects.status` is the only queue state there is. That keeps
deployment to one process and no broker. The cost is that work in flight does not survive a
restart: a project caught mid-analysis stays in its in-flight status, and because re-analysis
is refused while a run is in flight, it cannot be re-run — only deleted. Celery or RQ is the
path when that matters.

**git through `subprocess`, not a library.** The log is verified with stock `git` by people
who have never heard of SkillChain, so it is written with stock `git` too. There is no
second implementation of the object format whose output could differ from what a verifier's
`git` expects. SSH signing has been built into git since 2.34.

**The OpenTimestamps library, not its command-line client.** The `ots` client imports
python-bitcoinlib's OpenSSL bindings, which fail to load on Windows. Stamping a digest and
upgrading a proof need only the library, which has no such dependency.

**Removed.** The original dependency list carried the whole Ethereum toolchain — `web3`,
`eth-account` and their dependencies, around twenty packages — for badges minted on a testnet.
They went with the badges and the wallet. The list itself had been saved as UTF-16 by a shell
redirect, so `pip install -r` could not read it; it was rewritten as direct dependencies only.

## 2.3 Why git and Bitcoin instead of a blockchain

The blockchain layer SkillChain started with was doing two separate jobs:

1. **Ownership** — "this badge belongs to wallet `0xabc`". This is the only reason a wallet
   existed.
2. **Integrity in time** — "this record existed at this moment, and has not changed since".

The first is dropped. A credential bound permanently to a wallet is a poor fit for employment:
it publishes a sensitive claim with no good way to revoke it, and it makes the *recruiter* —
the person doing the verifying — install and understand chain tooling. The testnet it lived on
made no promise of permanence either; testnets are reset.

The second is worth keeping, and is done better by two layers that each do one thing.

### Who said it: a signed git log

Git is, structurally, what blockchains reinvented. It is a content-addressed, append-only
Merkle graph: a commit's identifier is a hash over its content *and* its parent's identifier,
so changing any past commit changes the identifier of every commit after it. A rewrite cannot
hide.

Each report is committed to its own log with an SSH signature. The signature lives inside the
commit object, so checking it needs only the commit and the public key. No host has authority
over that check — not GitHub, not SkillChain — and the log is pushed to several independent
remotes, so no single host can make it disappear. A remote is a mirror, not a source of truth.

### When: Bitcoin, through OpenTimestamps

A signature proves who issued a report, but the signer controls the clock. OpenTimestamps
removes that. Its public calendar servers gather many hashes into a Merkle tree and commit its
root to a Bitcoin transaction. The proof that comes back is the path from the report's hash to
a Bitcoin block header, and it can be checked against any Bitcoin node.

There is no wallet, no transaction fee paid by SkillChain, no smart contract and no API key.
Only a hash leaves the server, and not even the report's own: the report's hash is combined
with a random nonce and hashed again before it is sent, so a calendar cannot link what it
receives to a published report.

### Together

The signature answers *who*, immediately. The anchor answers *when*, a few hours later, once
Bitcoin confirms. Neither depends on any git host staying up, and the anchor does not depend
on trusting SkillChain's clock.

SkillChain holds the signing key, so it could sign a different report. What it cannot do is
make that report the *same* one. Altered bytes have a different hash, sit at a different path
in the log, and can only be anchored at a later block; the original stays in every mirror
that already holds it, with its earlier anchor. The guarantee is not that a report can never
be replaced, but that a replacement can never pass for the original.

This is the same shape the software supply-chain world settled on. Sigstore's Rekor is an
append-only transparency log with signed checkpoints, and it too chose a verifiable log over a
blockchain.

### What this does not prove

The log proves a report was issued by the holder of the signing key, has not changed since,
and existed by a given Bitcoin block. It does not prove the verdict is right. That is what the
span-level evidence is for: the reader opens the cited lines and judges for themselves.

## 2.4 Sources

- Git, [`git verify-commit`](https://git-scm.com/docs/git-verify-commit) — how a signed commit
  is checked.
- [OpenTimestamps](https://opentimestamps.org/) — the timestamping protocol and its public
  calendars.
- Sigstore, [Rekor transparency log](https://docs.sigstore.dev/logging/overview/) — an
  append-only log used for software supply-chain signing.
- Weber-Wulff et al., [Testing of Detection Tools for AI-Generated Text](https://arxiv.org/abs/2306.15666)
  (2023) — fourteen detection tools found neither accurate nor reliable. Cited in
  [10](10-authorship-signals.md).
- Liang et al., [GPT detectors are biased against non-native English writers](https://arxiv.org/abs/2304.02819)
  (2023) — the misclassification of non-native writing. Cited in
  [10](10-authorship-signals.md).
