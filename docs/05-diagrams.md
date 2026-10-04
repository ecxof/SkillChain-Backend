# 5. UML diagrams

Three views of the system: who uses it and for what (use case), what happens when a project is
analysed (activity), and how the code is organised (class). Each is drawn from the code as it
stands, not from the plan it began as.

The diagrams are written in Mermaid so they live beside the code and change with it. Mermaid has
no native use case notation, so that diagram uses the common stand-in: actors as boxes, use cases
as rounded ovals inside a system boundary, and `«include»` relationships as dashed arrows.

## 5.1 Use case diagram

```mermaid
flowchart LR
    DEV["«actor»<br/>Developer"]
    REC["«actor»<br/>Recruiter<br/>(no account)"]
    ADM["«actor»<br/>System administrator"]

    subgraph system["SkillChain"]
        direction TB
        UC_SIGN(["Sign in with Google or GitHub"])
        UC_LINK(["Link or unlink GitHub"])
        UC_SUBMIT(["Submit a project"])
        UC_TRACK(["Track analysis progress"])
        UC_READ(["Read own report"])
        UC_REDO(["Re-analyse a project"])
        UC_DEL(["Delete a project"])
        UC_PROF(["Publish or withdraw profile"])

        UC_ANALYSE(["Analyse repository"])
        UC_ATTEST(["Sign and commit report"])
        UC_STAMP(["Timestamp report in Bitcoin"])

        UC_VIEWP(["View public profile"])
        UC_VIEWR(["Read public report"])
        UC_PROOF(["Fetch attestation proof"])
        UC_VSIG(["Verify report signature"])
        UC_VTIME(["Verify Bitcoin timestamp"])

        UC_KEY(["Hold the signing key"])
        UC_MIRROR(["Configure log mirrors"])
        UC_UPGRADE(["Run timestamp upgrade job"])
    end

    GH["«system»<br/>GitHub API"]
    LLM["«system»<br/>LLM provider"]
    OTS["«system»<br/>OpenTimestamps calendars"]

    DEV --- UC_SIGN
    DEV --- UC_LINK
    DEV --- UC_SUBMIT
    DEV --- UC_TRACK
    DEV --- UC_READ
    DEV --- UC_REDO
    DEV --- UC_DEL
    DEV --- UC_PROF

    REC --- UC_VIEWP
    REC --- UC_VIEWR
    REC --- UC_VSIG
    REC --- UC_VTIME

    ADM --- UC_KEY
    ADM --- UC_MIRROR
    ADM --- UC_UPGRADE

    UC_SUBMIT -.->|"«include»"| UC_ANALYSE
    UC_REDO -.->|"«include»"| UC_ANALYSE
    UC_ANALYSE -.->|"«include»"| UC_ATTEST
    UC_ATTEST -.->|"«include»"| UC_STAMP
    UC_VSIG -.->|"«include»"| UC_PROOF
    UC_VTIME -.->|"«include»"| UC_PROOF

    UC_SIGN --- GH
    UC_ANALYSE --- GH
    UC_ANALYSE --- LLM
    UC_STAMP --- OTS
    UC_UPGRADE --- OTS
```

**Actors.**

- **Developer** — the primary user. Signs in, submits repositories, reads what was said about
  them, and decides whether to publish.
- **Recruiter** — reads a profile and the reports behind it without an account, and can verify
  any report independently. Verification happens on the recruiter's own machine with stock git;
  SkillChain's part is serving the proof and the public key.
- **System administrator** — holds the signing key, chooses the mirrors, and schedules the job
  that completes Bitcoin anchors.
- **GitHub API, LLM provider, OpenTimestamps calendars** — external systems the analysis
  depends on. None of them is trusted for the integrity of a report: the evidence is checked
  against the snapshot, and the signature and anchor are checked by the reader.

**Relationships.** Submitting and re-analysing both include the analysis, which includes
publishing the report, which includes timestamping it. Verifying a signature and verifying a
timestamp both include fetching the proof from `/public/reports/{id}/attestation`.

## 5.2 Activity diagram — analysing a project

From submission to a published report, including every failure branch and the anchor that
completes hours later.

```mermaid
flowchart TD
    START((" ")) --> SUBMIT["Submit repository URL, claimed skills, role"]
    SUBMIT --> VALID{"Request valid?"}
    VALID -- no --> R422["422: field errors returned"] --> STOP1(((" ")))
    VALID -- yes --> CREATE["Create project as pending, respond 202"]
    CREATE --> FETCHING["Status: fetching"]

    FETCHING --> TOKEN{"GitHub linked?"}
    TOKEN -- yes --> AUTH["Fetch with the user's token, 5000 per hour"]
    TOKEN -- no --> PUB["Fetch unauthenticated, 60 per hour"]
    AUTH --> GHOK{"GitHub answer"}
    PUB --> GHOK
    GHOK -- 404 --> F_NF["Failed: missing, or private"]
    GHOK -- no commits --> F_EMPTY["Failed: empty repository"]
    GHOK -- token rejected --> F_CRED["Failed: stored token rejected"]
    GHOK -- rate limited --> F_RATE["Failed: limit reached, resets at a time"]
    GHOK -- ok --> SNAP["Store snapshot pinned to one commit"]

    SNAP --> ANALYZING["Status: analyzing"]
    ANALYZING --> SIGNALS["Derive the five authorship signals"]
    SIGNALS --> ASK["Ask the model, with line-numbered files"]
    ASK --> USABLE{"Usable reply?"}
    USABLE -- no --> LEFT1{"Attempt left?"}
    LEFT1 -- yes --> ASK
    LEFT1 -- no --> F_MODEL["Failed: model returned nothing usable"]
    USABLE -- yes --> SPANS["Check every cited span against the snapshot"]
    SPANS --> RESOLVE{"All citations resolve?"}
    RESOLVE -- no --> LEFT2{"Attempt left?"}
    LEFT2 -- yes --> CORRECT["Ask again, listing the failed citations"] --> USABLE
    LEFT2 -- no --> DROP["Drop unresolved citations; a skill left with none is unverified"]
    RESOLVE -- yes --> STORE
    DROP --> STORE["Store report and one assessment per skill"]

    STORE --> ATTESTING["Status: attesting"]
    ATTESTING --> HASH["Canonical JSON and content hash"]
    HASH --> ATT_ON{"Attestation enabled?"}
    ATT_ON -- no --> A_PEND["Attestation stays pending"] --> DONE
    ATT_ON -- yes --> COMMIT{"Signed commit made?"}
    COMMIT -- no --> A_FAIL["Attestation failed"] --> DONE
    COMMIT -- yes --> PUSH["Push to each mirror, best effort"]
    PUSH --> ANY{"Any mirror accepted?"}
    ANY -- yes --> A_MIR["Attestation mirrored"]
    ANY -- no --> A_COM["Attestation committed"]
    A_MIR --> OTS_ON
    A_COM --> OTS_ON{"Timestamping enabled?"}
    OTS_ON -- no --> DONE
    OTS_ON -- yes --> STAMP{"A calendar accepted the digest?"}
    STAMP -- no --> T_FAIL["Timestamp failed"] --> DONE
    STAMP -- yes --> PROOF["Commit the pending proof; timestamp pending"] --> DONE

    DONE["Status: completed"] --> STOP2(((" ")))

    F_NF --> FAILED
    F_EMPTY --> FAILED
    F_CRED --> FAILED
    F_RATE --> FAILED
    F_MODEL --> FAILED
    FAILED["Status: failed, with a message for the submitter"] --> STOP3(((" ")))

    subgraph later["Hours later: the upgrade job, each hour"]
        direction TB
        J_READ["Read the pending proof from the log"] --> J_ANCH{"Bitcoin confirmed?"}
        J_ANCH -- not yet --> J_WAIT["Leave pending; retry next run"]
        J_ANCH -- yes --> J_COMMIT{"Completed proof committed?"}
        J_COMMIT -- yes --> J_DONE["Timestamp anchored, with block height"]
        J_COMMIT -- no --> J_WAIT
    end
    PROOF -.-> J_READ
```

**Two attempts, two purposes.** The model is asked at most twice. An unusable reply — one that
cannot be parsed, or a provider error — is retried with the same prompt; if the second reply is
also unusable, the run fails. Citations that do not resolve are retried with a note listing
exactly which ones failed and why. The two share the budget, so a run whose first reply was
unusable gets no correction round.

**Publishing never fails the run.** Every branch after the report is stored leads to
`completed`. The failures are recorded on the report, and the project stays readable.

**Any unexpected exception** at any stage ends in `failed` with a generic message, the detail
going to the server log. It is left off the diagram because it can happen anywhere.

## 5.3 Class diagrams

The code is Python modules more than classes, and the diagrams say so rather than inventing
classes that do not exist. Service modules are drawn with the `«module»` stereotype and their
public functions; `LLMProvider` is drawn as an `«interface»` because it is a `Protocol`, which
any class with a matching `complete` method satisfies without inheriting from it.

### Domain model

The persistent entities, as mapped by SQLAlchemy. The full column list and constraints are in
[04](04-database.md).

```mermaid
classDiagram
    direction LR

    class User {
        +str id
        +str email
        +str google_id
        +str github_id
        +str github_username
        +str profile_slug
        +bool profile_is_public
    }

    class Project {
        +str id
        +str github_repo_url
        +str repo_owner
        +str repo_name
        +list skills_claimed
        +str role_in_project
        +str status
        +str error_message
        +str visibility
    }

    class RepoSnapshot {
        +str id
        +str commit_sha
        +dict file_tree
        +dict sampled_files
        +list commit_history
        +list contributor_stats
        +str fetch_mode
        +datetime fetched_at
    }

    class AnalysisReport {
        +str id
        +str model_name
        +str prompt_version
        +int overall_trust_score
        +str summary
        +dict authorship_signals
        +str content_hash
        +str attestation_commit_sha
        +str attestation_status
        +str timestamp_status
        +int bitcoin_block_height
    }

    class SkillAssessment {
        +str skill_name
        +bool claimed
        +bool verified
        +str level
        +int confidence
        +str reason
        +list evidence
    }

    class EvidenceSpan {
        <<value object>>
        +str type
        +str detail
        +str file
        +int start_line
        +int end_line
    }

    User "1" *-- "0..*" Project : submits
    Project "1" *-- "0..*" RepoSnapshot : fetched as
    RepoSnapshot "1" *-- "0..1" AnalysisReport : analysed into
    AnalysisReport "1" *-- "1..*" SkillAssessment : contains
    SkillAssessment "1" o-- "0..*" EvidenceSpan : cites
```

Composition runs down the chain because deletion does: removing a user removes their projects,
and so on to the assessments, enforced by the database. An `EvidenceSpan` is not a table — it is
validated by a Pydantic model and stored inside the assessment's `evidence` column.

### Services and their collaborators

```mermaid
classDiagram
    direction TB

    class analysis_service {
        <<module>>
        +run_pipeline(project_id, session_factory, provider, log) str
    }

    class github_service {
        <<module>>
        +fetch_repository(owner, name, token, submitter_login) RepositoryData
        +select_files(entries, max_files, max_bytes) list
    }

    class authorship_service {
        <<module>>
        +derive_authorship_signals(snapshot, github_username) dict
    }

    class ai_service {
        <<module>>
        +analyze_snapshot(snapshot, skills_claimed, role, provider) AnalysisResult
        +validate_evidence(raw, line_counts, skill_name) tuple
        +get_provider() LLMProvider
    }

    class attestation_service {
        <<module>>
        +build_document(report) dict
        +canonical_json(document) bytes
        +content_hash(canonical) str
        +attest(report, log) AttestationRecord
        +verify_commands(record) list
        +from_env() AttestationLog
    }

    class timestamp_service {
        <<module>>
        +stamp(digest) bytes
        +upgrade(proof) tuple
        +anchor_of(proof) BitcoinAnchor
        +digest_of(proof) bytes
    }

    class profile_service {
        <<module>>
        +get_public_profile(session, slug) PublicProfileOut
        +newest_report(project) AnalysisReport
    }

    class LLMProvider {
        <<interface>>
        +str model_name
        +complete(prompt) LLMResponse
    }

    class XAIProvider {
        +str model_name
        +complete(prompt) LLMResponse
    }

    class AttestationLog {
        +Path repo_path
        +Path signing_key
        +list remotes
        +append(document) AttestationRecord
        +append_proof(digest, proof) str
        +read_proof(digest) bytes
        +push() list
        +allowed_signers() str
    }

    class AttestationRecord {
        <<dataclass>>
        +str content_hash
        +str commit_sha
        +str path
        +bytes canonical_json
        +bool signed
        +list mirrors
    }

    class RepositoryData {
        <<dataclass>>
        +str commit_sha
        +dict sampled_files
        +snapshot_fields() dict
    }

    class AnalysisResult {
        <<dataclass>>
        +int overall_trust_score
        +list skills
        +str model_name
        +str prompt_version
        +int attempts
    }

    class Signal {
        <<dataclass>>
        +str kind
        +str observation
        +dict data
    }

    class BitcoinAnchor {
        <<dataclass>>
        +int block_height
    }

    class GitHubError
    class RepositoryNotFound
    class EmptyRepository
    class BadCredentials
    class RateLimited

    analysis_service ..> github_service : fetches with
    analysis_service ..> authorship_service : derives signals with
    analysis_service ..> ai_service : analyses with
    analysis_service ..> attestation_service : publishes with
    analysis_service ..> timestamp_service : stamps with

    github_service ..> RepositoryData : returns
    authorship_service ..> Signal : builds
    ai_service ..> LLMProvider : calls
    ai_service ..> AnalysisResult : returns
    XAIProvider ..|> LLMProvider
    attestation_service ..> AttestationLog : writes through
    AttestationLog ..> AttestationRecord : returns
    timestamp_service ..> BitcoinAnchor : reads

    GitHubError <|-- RepositoryNotFound
    GitHubError <|-- EmptyRepository
    GitHubError <|-- BadCredentials
    GitHubError <|-- RateLimited
    github_service ..> GitHubError : raises
```

`analysis_service` depends on every other analysis service and nothing depends on it except the
routes, which is what makes it the orchestrator. Each of the others can be — and is — tested
without the rest. `profile_service` stands apart: it reads what the pipeline wrote and never
calls into it.
