"""The signed, append-only log that makes a report checkable without trusting us.

Each finished report is serialised to canonical JSON, addressed by the sha256
of those exact bytes, and committed to a git repository with an SSH signature.
The commit SHA becomes the report's permanent public ID.

Git is the whole mechanism, and deliberately so. A commit SHA hashes the tree
*and* the parent commit, so rewriting any earlier report changes every SHA that
follows it; the signature lives inside the commit object, so verifying one needs
only the commit and the public key. No host has authority over that, which is
why ``ATTESTATION_REMOTES`` takes a list: mirrored to several independent hosts,
no single one can quietly drop the log.

Everything here is synchronous and blocks on ``git``. The pipeline is async, so
callers should reach it through ``asyncio.to_thread``. Nothing here may lose an
analysis: callers treat :class:`AttestationError` as a recorded failure on the
report, never as a failed run.
"""

import hashlib
import json
import logging
import os
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Bumped if the published document's shape ever changes, so old attestations
# stay interpretable against the schema they were written under.
DOCUMENT_SCHEMA = "skillchain.report/v1"

# Identity on the attestation commits. It is also the principal an
# allowed_signers file must name, so it is a constant rather than a setting:
# changing it would invalidate verification of every earlier commit.
SIGNER_NAME = "SkillChain Attestation"
SIGNER_IDENTITY = "attestation@skillchain"

GIT_TIMEOUT = 30.0
PUSH_TIMEOUT = 60.0

# One writer at a time. The log is append-only and its commits are a chain, so
# two concurrent analyses must not race to build on the same parent.
_WRITE_LOCK = threading.Lock()


class AttestationError(RuntimeError):
    """The report could not be committed to the log.

    Recorded as ``attestation_status='failed'`` on the report, which still
    saves and still serves; the analysis behind it is never discarded.
    """


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def is_enabled() -> bool:
    """Whether reports should be attested at all.

    Off by default so local development and tests need no signing key and
    touch no network.
    """
    return _flag("ATTESTATION_ENABLED")


# --- Canonical form ----------------------------------------------------------

def canonical_json(document: dict) -> bytes:
    """Serialise a document to the exact bytes that get hashed and committed.

    Keys sorted, no insignificant whitespace, UTF-8, one trailing newline.
    Re-serialising the same document has to produce identical bytes, because
    the sha256 of these bytes is the report's address; anything that varies
    between runs or machines would change the hash and break verification.
    The attestation repository also disables git's end-of-line translation, so
    a Windows checkout cannot rewrite them either.
    """
    text = json.dumps(document, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return text.encode("utf-8") + b"\n"


def content_hash(canonical: bytes) -> str:
    return hashlib.sha256(canonical).hexdigest()


def report_path(digest: str) -> str:
    """Content-addressed location, sharded the way git shards its own objects.

    One flat directory would hold hundreds of thousands of entries eventually,
    which is slow to list on most filesystems.
    """
    return f"reports/{digest[:2]}/{digest}.json"


def proof_path(digest: str) -> str:
    """Where a report's OpenTimestamps proof lives, beside the report itself."""
    return f"reports/{digest[:2]}/{digest}.ots"


def _iso(value: datetime | None) -> str | None:
    """UTC ISO-8601, treating a naive datetime as UTC.

    SQLite hands back naive datetimes where PostgreSQL returns aware ones; the
    published bytes must not depend on which database produced them.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def build_document(report) -> dict:
    """The published form of a report, from an AnalysisReport with relations loaded.

    Carries everything a reader needs to check the verdict and re-run it, and
    nothing else. The raw model response and token usage are internal; the
    submitter appears only as their public GitHub username, since this document
    may be pushed to public mirrors.
    """
    snapshot = report.snapshot
    project = snapshot.project
    user = getattr(project, "user", None)
    skills = sorted(report.skills, key=lambda s: (not s.claimed, s.skill_name.casefold()))

    return {
        "schema": DOCUMENT_SCHEMA,
        "report_id": report.id,
        "issued_at": _iso(report.created_at),
        "repository": {
            "url": project.github_repo_url,
            "owner": project.repo_owner,
            "name": project.repo_name,
            "commit_sha": snapshot.commit_sha,
            "default_branch": snapshot.default_branch,
        },
        "submitter": {
            "github_username": getattr(user, "github_username", None),
            "role_in_project": project.role_in_project,
        },
        "provenance": {
            "model": report.model_name,
            "prompt_version": report.prompt_version,
            "fetch_mode": snapshot.fetch_mode,
            "fetched_at": _iso(snapshot.fetched_at),
        },
        "overall_trust_score": report.overall_trust_score,
        "summary": report.summary,
        "skills": [
            {
                "skill_name": skill.skill_name,
                "claimed": skill.claimed,
                "verified": skill.verified,
                "level": skill.level,
                "confidence": skill.confidence,
                "reason": skill.reason,
                "evidence": skill.evidence or [],
            }
            for skill in skills
        ],
        "authorship_signals": report.authorship_signals,
    }


# --- The log -----------------------------------------------------------------

@dataclass
class AttestationRecord:
    """What one successful append produced."""

    content_hash: str
    commit_sha: str
    path: str
    canonical_json: bytes
    signed: bool
    attested_at: datetime
    mirrors: list[str] = field(default_factory=list)


class AttestationLog:
    """A git repository holding one signed commit per report.

    ``signing_key`` is the path to an SSH private key. It is optional here so
    the mechanics can be exercised without one, but :func:`from_env` refuses to
    build an unsigned log: an unsigned append-only log proves far less, and
    silently degrading to one would undermine the only claim it exists to make.
    """

    def __init__(self, repo_path, *, signing_key=None, remotes=()):
        self.repo_path = Path(repo_path)
        self.signing_key = Path(signing_key) if signing_key else None
        self.remotes = [remote for remote in remotes if remote]

    # -- git plumbing --

    def _git(self, *args: str, timeout: float = GIT_TIMEOUT,
             check: bool = True) -> subprocess.CompletedProcess:
        environment = {
            **os.environ,
            # Never block waiting for credentials on a push; fail and move on.
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
        }
        try:
            result = subprocess.run(
                ["git", *args], cwd=self.repo_path, capture_output=True,
                text=True, timeout=timeout, env=environment,
            )
        except FileNotFoundError as exc:
            raise AttestationError("git is not installed or not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise AttestationError(f"git {args[0]} timed out after {timeout}s") from exc
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise AttestationError(f"git {args[0]} failed: {detail}")
        return result

    def ensure_initialised(self) -> None:
        """Create the repository and its configuration if it is not there yet."""
        self.repo_path.mkdir(parents=True, exist_ok=True)
        if not (self.repo_path / ".git").exists():
            self._git("init", "--quiet", "--initial-branch=main")

        self._git("config", "user.name", SIGNER_NAME)
        self._git("config", "user.email", SIGNER_IDENTITY)
        # The committed bytes must be exactly the bytes that were hashed, so
        # nothing may translate line endings on the way in or out.
        self._git("config", "core.autocrlf", "false")
        if self.signing_key:
            self._git("config", "gpg.format", "ssh")
            self._git("config", "user.signingkey", str(self.signing_key))

        attributes = self.repo_path / ".gitattributes"
        if not attributes.exists():
            attributes.write_bytes(b"reports/** -text\n")
            self._git("add", ".gitattributes")
            # Signed like every other commit, so "every commit in this log is
            # signed" holds without an exception for the root commit.
            bootstrap = ["commit", "--quiet", "-m",
                         "Record reports with no end-of-line translation"]
            if self.signing_key:
                bootstrap.insert(1, "-S")
            self._git(*bootstrap)

    def _committed_sha(self, path: str) -> str | None:
        """The commit that last touched ``path``, or None if it is untracked."""
        result = self._git("log", "-1", "--format=%H", "--", path, check=False)
        sha = result.stdout.strip()
        return sha or None

    def append(self, document: dict) -> AttestationRecord:
        """Write one report into the log as a signed commit.

        Idempotent by content: re-attesting a byte-identical document returns
        the commit that already holds it rather than making an empty one, so a
        retry after a partial failure cannot fork the log.
        """
        canonical = canonical_json(document)
        digest = content_hash(canonical)
        relative = report_path(digest)
        absolute = self.repo_path / relative

        with _WRITE_LOCK:
            self.ensure_initialised()

            if absolute.exists() and absolute.read_bytes() == canonical:
                existing = self._committed_sha(relative)
                if existing:
                    return AttestationRecord(
                        content_hash=digest, commit_sha=existing, path=relative,
                        canonical_json=canonical, signed=bool(self.signing_key),
                        attested_at=datetime.now(timezone.utc),
                    )

            absolute.parent.mkdir(parents=True, exist_ok=True)
            absolute.write_bytes(canonical)

            self._git("add", "--", relative)
            commit_args = ["commit", "--quiet", "-m", _commit_message(document, digest)]
            if self.signing_key:
                commit_args.insert(1, "-S")
            self._git(*commit_args)

            commit_sha = self._git("rev-parse", "HEAD").stdout.strip()

        return AttestationRecord(
            content_hash=digest, commit_sha=commit_sha, path=relative,
            canonical_json=canonical, signed=bool(self.signing_key),
            attested_at=datetime.now(timezone.utc),
        )

    def append_proof(self, digest: str, proof: bytes) -> str:
        """Commit a report's OpenTimestamps proof and return the commit SHA.

        Proofs arrive twice: pending at issue time, then upgraded hours later
        once Bitcoin confirms. Both are ordinary commits, which is exactly what
        an append-only log is for — the upgrade is recorded rather than
        overwriting the evidence that the earlier, weaker proof existed.
        """
        relative = proof_path(digest)
        absolute = self.repo_path / relative

        with _WRITE_LOCK:
            self.ensure_initialised()
            if absolute.exists() and absolute.read_bytes() == proof:
                existing = self._committed_sha(relative)
                if existing:
                    return existing

            absolute.parent.mkdir(parents=True, exist_ok=True)
            absolute.write_bytes(proof)
            self._git("add", "--", relative)
            commit_args = ["commit", "--quiet", "-m",
                           f"Timestamp report {digest[:12]}\n\ncontent-hash: {digest}\n"]
            if self.signing_key:
                commit_args.insert(1, "-S")
            self._git(*commit_args)
            return self._git("rev-parse", "HEAD").stdout.strip()

    def read_proof(self, digest: str) -> bytes | None:
        """A report's stored proof, or None if it was never stamped."""
        stored = self.repo_path / proof_path(digest)
        return stored.read_bytes() if stored.exists() else None

    def push(self) -> list[str]:
        """Mirror the log to every configured remote, best effort.

        Returns the remotes that accepted it. A mirror being unreachable is
        not a failure of the attestation: the signed commit already exists
        locally and can be pushed later.
        """
        pushed = []
        for remote in self.remotes:
            with _WRITE_LOCK:
                result = self._git("push", remote, "HEAD:refs/heads/main",
                                   timeout=PUSH_TIMEOUT, check=False)
            if result.returncode == 0:
                pushed.append(remote)
            else:
                logger.warning("attestation mirror %s rejected the push: %s",
                               remote, (result.stderr or "").strip())
        return pushed

    def public_key(self) -> str | None:
        """The signing key's public half, as stored beside the private key."""
        if not self.signing_key:
            return None
        public = self.signing_key.with_suffix(self.signing_key.suffix + ".pub")
        if not public.exists():
            public = Path(str(self.signing_key) + ".pub")
        return public.read_text(encoding="utf-8").strip() if public.exists() else None

    def allowed_signers(self) -> str | None:
        """The key in OpenSSH allowed_signers form, for ``git verify-commit``.

        Served at /.well-known/skillchain-signing-key so anyone can check a
        signature without having to ask us for the key first.
        """
        key = self.public_key()
        return f"{SIGNER_IDENTITY} {key}\n" if key else None


def _commit_message(document: dict, digest: str) -> str:
    repository = document.get("repository") or {}
    full_name = f"{repository.get('owner')}/{repository.get('name')}"
    return (
        f"Attest report {digest[:12]} for {full_name}\n"
        f"\n"
        f"content-hash: {digest}\n"
        f"repo-commit: {repository.get('commit_sha')}\n"
        f"model: {(document.get('provenance') or {}).get('model')}\n"
        f"prompt-version: {(document.get('provenance') or {}).get('prompt_version')}\n"
    )


def from_env() -> AttestationLog:
    """Build the configured log, refusing to run unsigned.

    Raises :class:`AttestationError` rather than degrading, since an
    attestation nobody can verify is worse than an honest failure.
    """
    repo_path = os.getenv("ATTESTATION_REPO_PATH")
    if not repo_path:
        raise AttestationError("ATTESTATION_REPO_PATH is not set")
    signing_key = os.getenv("ATTESTATION_SIGNING_KEY_PATH")
    if not signing_key:
        raise AttestationError(
            "ATTESTATION_SIGNING_KEY_PATH is not set; attestation needs a signing key"
        )
    if not Path(signing_key).exists():
        raise AttestationError(f"the signing key {signing_key} does not exist")
    remotes = [part.strip() for part in (os.getenv("ATTESTATION_REMOTES") or "").split(",")]
    return AttestationLog(repo_path, signing_key=signing_key, remotes=remotes)


def attest(report, *, log: AttestationLog | None = None) -> AttestationRecord:
    """Publish one report to the log and mirror it.

    The caller records the result on the report and treats an
    :class:`AttestationError` as ``attestation_status='failed'``.
    """
    log = log or from_env()
    record = log.append(build_document(report))
    record.mirrors = log.push()
    return record


def verify_commands(record: AttestationRecord, mirror: str | None = None) -> list[str]:
    """Copy-pasteable commands proving the report, for the attestation endpoint."""
    source = mirror or (record.mirrors[0] if record.mirrors else "<attestation-repo-url>")
    return [
        f"git clone {source} skillchain-attestations",
        f"cd skillchain-attestations && git verify-commit {record.commit_sha}",
        f"sha256sum {record.path}  # expect {record.content_hash}",
    ]
