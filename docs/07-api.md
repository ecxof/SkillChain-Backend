# 7. API

The API is JSON over HTTP, served by FastAPI. Interactive documentation generated from the
same code is at `/docs` on a running server; this document is the narrative version, with the
reasons behind the behaviour that the generated reference cannot show.

## 7.1 Conventions

**Authentication.** Endpoints marked *Bearer* need an `Authorization: Bearer <token>` header.
The token is a JWT issued by the OAuth callbacks and valid for 60 minutes. A missing, malformed
or expired token is answered `401` with a `WWW-Authenticate: Bearer` header.

**Missing, not forbidden.** A project owned by someone else, and a report or profile that has
not been published, are answered `404` — never `403`. A `403` would confirm the resource exists
to a caller with no right to know that. A stale link and a guessed identifier get the same
answer.

**Errors** carry a `detail` field. Validation failures are `422` with FastAPI's standard list of
field errors. The other statuses used:

| Status | Meaning here |
|---|---|
| `400` | The OAuth provider rejected the code, or a profile was published without an address |
| `401` | No valid bearer token |
| `404` | Not found, or not yours, or not published |
| `409` | Conflicts with current state: a taken profile address, a re-analysis already running, or unlinking the only sign-in method |
| `422` | The request body failed validation |

**Slow work returns `202`.** Submitting or re-analysing a project creates the record and starts
the analysis in the background. The response describes the project as it is now; the client
follows progress by polling `GET /projects/{id}`.

**Timestamps** are ISO 8601. **Identifiers** are UUID strings.

**CORS** allows the frontend's development origins, `http://localhost:3000` and
`http://localhost:5173`.

## 7.2 Endpoints at a glance

| Method | Path | Auth | Success | Purpose |
|---|---|---|---|---|
| `GET` | `/` | — | `200` | Health check |
| `GET` | `/.well-known/skillchain-signing-key` | — | `200` | The key that signs the attestation log |
| `GET` | `/auth/google/url` | — | `200` | Where to send the browser to sign in with Google |
| `POST` | `/auth/google/callback` | — | `200` | Exchange Google's code for a token |
| `GET` | `/auth/github/url` | — | `200` | Where to send the browser to sign in with GitHub |
| `POST` | `/auth/github/callback` | — | `200` | Exchange GitHub's code for a token |
| `GET` | `/auth/me` | Bearer | `200` | The signed-in account |
| `PATCH` | `/auth/me/profile` | Bearer | `200` | Choose a profile address; publish or withdraw it |
| `DELETE` | `/auth/github/unlink` | Bearer | `200` | Forget the GitHub link and token |
| `POST` | `/projects` | Bearer | `202` | Submit a repository for analysis |
| `GET` | `/projects` | Bearer | `200` | The caller's projects |
| `GET` | `/projects/{id}` | Bearer | `200` | One project, its status, and its newest report |
| `GET` | `/projects/{id}/report` | Bearer | `200` | The newest report in full |
| `POST` | `/projects/{id}/reanalyze` | Bearer | `202` | Run the analysis again |
| `DELETE` | `/projects/{id}` | Bearer | `204` | Delete a project, its snapshots and reports |
| `GET` | `/public/profiles/{slug}` | — | `200` | A published skill profile |
| `GET` | `/public/reports/{id}` | — | `200` | A report on a public project |
| `GET` | `/public/reports/{id}/attestation` | — | `200` | Everything needed to verify that report |

## 7.3 Authentication

Sign-in is OAuth 2.0 in two steps, with the frontend in the middle so the API never handles a
browser redirect itself.

1. The frontend asks the API for the provider's consent URL and sends the browser there.
2. The provider redirects back to the frontend at `{FRONTEND_URL}/auth/{provider}/callback`
   with a `code`. The frontend posts that code to the API and receives a SkillChain token.

### `GET /auth/google/url` and `GET /auth/github/url`

```json
{ "url": "https://github.com/login/oauth/authorize?client_id=…&scope=read:user user:email&redirect_uri=…" }
```

Google asks for `openid email profile`. GitHub asks for `read:user user:email`, which identifies
the user and raises their GitHub rate limit from 60 to 5,000 requests an hour, but does **not**
grant access to private repositories.

### `POST /auth/google/callback` and `POST /auth/github/callback`

Request:

```json
{ "code": "the code the provider returned" }
```

Response:

```json
{ "access_token": "eyJhbGciOiJIUzI1NiIs…", "token_type": "bearer" }
```

Errors: `400` if the provider rejects the code.

**How accounts are matched.** An account is found by the provider's own identifier first, then
by email address, and created only if neither matches. Signing in with GitHub under the same
email as an existing Google account therefore links GitHub to that account rather than creating
a second one. For GitHub, the primary address is read from `/user/emails` when the profile's
email is private. Each GitHub sign-in refreshes the stored token.

### `GET /auth/me` — Bearer

```json
{
  "id": "b2e6a468-982c-4c47-b60e-956640afd8d4",
  "email": "ada@example.com",
  "display_name": "Ada Lovelace",
  "avatar_url": "https://avatars.githubusercontent.com/u/1",
  "github_username": "ada",
  "github_linked": true,
  "google_linked": false,
  "profile_slug": "ada",
  "profile_is_public": true,
  "created_at": "2026-09-29T17:51:11+00:00"
}
```

The stored GitHub access token is never returned; `github_linked` says whether one exists.

### `PATCH /auth/me/profile` — Bearer

Sets the profile address, publishes or withdraws the profile, or both. Fields left out are left
unchanged.

```json
{ "profile_slug": "ada", "profile_is_public": true }
```

| Field | Rules |
|---|---|
| `profile_slug` | 3–39 characters: lowercase letters, digits and single hyphens, not starting or ending with a hyphen. Stored lowercased. Words the frontend routes on — `admin`, `api`, `auth`, `dashboard`, `login`, `logout`, `me`, `new`, `profile`, `profiles`, `project`, `projects`, `public`, `report`, `reports`, `settings`, `signup`, `skillchain`, `verify` — are reserved |
| `profile_is_public` | Publishing requires an address |

Responds with the updated account, as `GET /auth/me`.

Errors:

- `409` — the address belongs to someone else.
- `400` — publishing without an address. A public profile with nowhere to be served would be
  silently invisible, so the request is refused rather than accepted as a no-op.
- `422` — an invalid or reserved slug, or an unknown field.

Withdrawing a profile keeps its address, so a profile that is published again returns at the
same URL.

### `DELETE /auth/github/unlink` — Bearer

Forgets the GitHub identifier, username and access token. Analyses already run are unaffected;
later ones fetch unauthenticated, at the lower rate limit.

Errors: `409` when GitHub is the account's only sign-in method, since removing it would lock the
account out of itself.

## 7.4 Projects

All project endpoints are Bearer, and all of them answer `404` for a project the caller does
not own.

### `POST /projects` — `202`

```json
{
  "title": "Portfolio API",
  "description": "The backend for my portfolio site.",
  "github_repo_url": "https://github.com/ada/portfolio",
  "live_demo_url": "https://ada.dev",
  "skills_claimed": ["Python", "FastAPI", "PostgreSQL"],
  "role_in_project": "Sole developer",
  "visibility": "public"
}
```

| Field | Rules |
|---|---|
| `title` | Required, 1–200 characters |
| `description` | Optional, up to 5,000 characters |
| `github_repo_url` | Required. Accepts the forms people paste: with or without `https://` or `www`, a trailing slash or `.git`, a query string, or an SSH clone URL (`git@github.com:owner/repo.git`). Refuses other hosts, URLs carrying credentials, and paths deeper than the repository root such as `/tree/main`. Stored as `https://github.com/{owner}/{repo}` |
| `live_demo_url` | Optional; `http` or `https` only |
| `skills_claimed` | Required, 1–15 names of up to 50 characters. Letters, digits and `+ # . / ( ) & -` are allowed, so `C++`, `C#`, `Node.js`, `CI/CD` and `Go (Golang)` all work; newlines and quotes are not, because skill names are placed in the model's prompt. Duplicates are removed case-insensitively, keeping the first spelling |
| `role_in_project` | Optional, up to 500 characters |
| `visibility` | `private` (the default) or `public` |

Unknown fields are rejected, so a client cannot set `status` or `user_id` by including them.

The response is the project as just created, with `status: "pending"`. The analysis starts
after the response is sent.

### `GET /projects`

The caller's projects, most recently submitted first, each shaped as below without
`latest_report`.

### `GET /projects/{id}` — the polling endpoint

```json
{
  "id": "e45e292b-e393-4d23-8369-4ad68b879c89",
  "title": "Portfolio API",
  "description": "The backend for my portfolio site.",
  "github_repo_url": "https://github.com/ada/portfolio",
  "repo_owner": "ada",
  "repo_name": "portfolio",
  "live_demo_url": "https://ada.dev",
  "skills_claimed": ["Python", "FastAPI", "PostgreSQL"],
  "role_in_project": "Sole developer",
  "status": "completed",
  "error_message": null,
  "visibility": "public",
  "submitted_at": "2026-09-29T17:51:11+00:00",
  "latest_report": { "…": "a report, as below" }
}
```

`status` moves through `pending`, `fetching`, `analyzing`, `attesting`, and ends at `completed`
or `failed`. A client should poll until it reaches one of the two. On `failed`, `error_message`
says why in words meant for the submitter.

`latest_report` is `null` until the pipeline has stored a report, which happens as the status
moves to `attesting`. During a re-analysis it continues to show the previous report until the
new one is stored.

### `GET /projects/{id}/report`

The newest report, whether or not the project is public: an owner needs to read what was said
about their work before deciding to publish it.

```json
{
  "id": "c6590cca-ffcc-4cd4-9f2a-e883d06c5eca",
  "project_id": "e45e292b-e393-4d23-8369-4ad68b879c89",
  "overall_trust_score": 82,
  "summary": "A small, well-tested FastAPI service.",
  "skills": [
    {
      "skill_name": "Python",
      "claimed": true,
      "verified": true,
      "level": "advanced",
      "confidence": 88,
      "reason": "Type hints and asyncio used throughout.",
      "evidence": [
        {
          "type": "source",
          "detail": "Async pipeline orchestration.",
          "file": "services/analysis_service.py",
          "start_line": 40,
          "end_line": 72
        },
        {
          "type": "languages",
          "detail": "Python is 94% of the repository by bytes.",
          "file": null,
          "start_line": null,
          "end_line": null
        }
      ]
    }
  ],
  "authorship": {
    "commits_analyzed": 41,
    "signals": [
      {
        "kind": "coauthor_trailers",
        "observation": "None of the 41 most recent commits carries a Co-authored-by trailer",
        "data": { "commits": 41, "coauthored": 0 }
      }
    ]
  },
  "provenance": {
    "repo_commit_sha": "a3f9c1b2e4d5f60718293a4b5c6d7e8f90a1b2c3",
    "default_branch": "main",
    "fetch_mode": "authenticated",
    "fetched_at": "2026-09-29T17:51:09+00:00",
    "model_name": "grok-3",
    "prompt_version": "2026-09-19.1"
  },
  "attestation": {
    "content_hash": "9d24bc9c089b610a331c2e1291fe85265a4338769a6904770d88fad969cf2a70",
    "commit_sha": "039458ab18d4593ec4f36524d7cfc414ac7880a1",
    "status": "mirrored",
    "attested_at": "2026-09-29T17:51:14+00:00",
    "timestamp_status": "pending",
    "anchored_at": null,
    "bitcoin_block_height": null
  },
  "created_at": "2026-09-29T17:51:13+00:00"
}
```

- **`evidence`** — `source`, `manifest` and `readme` evidence always carries `file`,
  `start_line` and `end_line`; `commits` and `languages` evidence describes the repository as a
  whole and never does. Every file citation was checked against the stored snapshot before the
  report was saved.
- **`authorship`** — always five signals, in a fixed order, so the absence of a finding is
  stated rather than left to be inferred. See [10](10-authorship-signals.md).
- **`provenance`** — what is needed to re-run the analysis: the exact commit, how it was
  fetched, the model and the prompt version.
- The model's raw response and token usage are stored but never returned.

Errors: `404` when the project has no report yet.

### `POST /projects/{id}/reanalyze` — `202`

Fetches the repository again — as it is now — and produces a new snapshot and a new report. The
earlier ones stay, including publicly, so a verdict that was shared cannot be quietly replaced.

Errors: `409` while the project is `pending`, `fetching`, `analyzing` or `attesting`. Two
pipelines on one project would race into the same columns.

### `DELETE /projects/{id}` — `204`

Deletes the project with its snapshots, reports and assessments. Reports already committed to
the attestation log stay there: the log is append-only, and a report that could be withdrawn on
request would prove nothing. What is lost is the route from a profile to them.

## 7.5 Public

None of these take a token. Each decides for itself what it may show.

### `GET /public/profiles/{slug}`

```json
{
  "slug": "ada",
  "display_name": "Ada Lovelace",
  "github_username": "ada",
  "avatar_url": "https://avatars.githubusercontent.com/u/1",
  "skills": [
    {
      "skill_name": "Python",
      "highest_level": "advanced",
      "projects_evidenced": 2,
      "report_ids": ["c6590cca-ffcc-4cd4-9f2a-e883d06c5eca", "7b1e0f3a-5c2d-4e8f-9a6b-1c3d5e7f9a0b"]
    }
  ]
}
```

Built on each request from the newest report of each of the user's projects that is both public
and completed. Only verified skills appear. A skill evidenced by several projects shows the
highest level any of them reached, and `report_ids` lists the reports a reader can open to see
the evidence.

Errors: `404` for an address nobody holds and for a profile that has not been published — the
two are indistinguishable by design.

### `GET /public/reports/{id}`

A report, shaped as `GET /projects/{id}/report`, if its project is public and completed.

Older reports of a public project stay readable. Re-analysis appends rather than replaces, and
serving only the newest would let a developer retire a verdict they had already shared.

Errors: `404` if the project is private, unfinished, or does not exist.

### `GET /public/reports/{id}/attestation`

Everything a reader needs to verify the report without trusting SkillChain. The fields of the
report's `attestation` block, plus:

| Field | Contents |
|---|---|
| `report_id` | The report this proves |
| `canonical_json` | The exact bytes that were hashed and committed. Rebuilt from the database on each request and re-hashed; if they no longer match `content_hash`, this is `null` rather than a false proof |
| `ots_proof` | The OpenTimestamps proof, base64-encoded, once one exists |
| `mirrors` | Where the attestation log can be cloned from. Empty unless a mirror accepted the report's commit |
| `verify_commands` | Commands to check the signature and the hash; see [7.7](#77-known-issues) |

A deployment with attestation switched off still answers, with the parts it has. The procedure
for checking a report by hand is in [09](09-verifying-a-report.md).

Errors: `404` on the same terms as the report.

## 7.6 Health and discovery

### `GET /`

```json
{ "status": "SkillChain API is running" }
```

### `GET /.well-known/skillchain-signing-key`

The attestation log's public key as plain text, in OpenSSH `allowed_signers` form:

```text
attestation@skillchain ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI…
```

It sits at the root, not under `/public`, because a well-known URI is one a verifier can find
from the domain alone. A deployment with no attestation configured answers `404`: nothing is
broken, and the caller did nothing wrong.

## 7.7 Known issues

- **The served verify commands omit the signing key.** `verify_commands` runs
  `git verify-commit` without pointing git at the published key, and git refuses to check an SSH
  signature without one:
  `error: gpg.ssh.allowedSignersFile needs to be configured and exist for ssh signature verification`.
  The commands also chain with `&&` and use `sha256sum`, neither of which works in Windows
  PowerShell 5.1. [09](09-verifying-a-report.md) gives a procedure that works on both.
- **A profile address cannot be cleared.** Sending `"profile_slug": null` is answered `409`
  with `The profile URL 'None' is already taken` whenever any other account has no address —
  which in practice is always. Withdrawing a profile with `"profile_is_public": false` works as
  documented.
