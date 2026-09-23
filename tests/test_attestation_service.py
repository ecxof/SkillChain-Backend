import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")

from services import attestation_service as att  # noqa: E402
from services.attestation_service import (  # noqa: E402
    AttestationError, AttestationLog, build_document, canonical_json, content_hash,
    from_env, report_path, verify_commands,
)

CREATED_AT = datetime(2026, 9, 20, 14, 30, tzinfo=timezone.utc)
FETCHED_AT = datetime(2026, 9, 20, 14, 29, tzinfo=timezone.utc)


def skill(name, *, claimed=True, verified=True, level="advanced", confidence=88):
    return SimpleNamespace(
        skill_name=name, claimed=claimed, verified=verified, level=level,
        confidence=confidence, reason=f"{name} is used throughout.",
        evidence=[{"type": "file", "file": "src/App.jsx", "start_line": 1,
                   "end_line": 20, "detail": "hooks"}],
    )


def make_report(**overrides):
    user = SimpleNamespace(github_username="octocat", email="octocat@example.test")
    project = SimpleNamespace(
        github_repo_url="https://github.com/octocat/portfolio",
        repo_owner="octocat", repo_name="portfolio",
        role_in_project="Sole developer", user=user,
    )
    snapshot = SimpleNamespace(
        project=project, commit_sha="a3f9c1b", default_branch="main",
        fetch_mode="public", fetched_at=FETCHED_AT,
    )
    report = SimpleNamespace(
        id="report-1", created_at=CREATED_AT, snapshot=snapshot,
        model_name="grok-3", prompt_version="v2", overall_trust_score=84,
        summary="A React portfolio.", authorship_signals={"copilot_trailers": 2},
        raw_response="{...}", token_usage={"total": 4096},
        skills=[skill("React"), skill("CSS", claimed=False)],
    )
    for key, value in overrides.items():
        setattr(report, key, value)
    return report


# --- Canonical form ----------------------------------------------------------

def test_canonical_json_sorts_keys_and_ends_with_one_newline():
    canonical = canonical_json({"b": 1, "a": {"d": 2, "c": 3}})
    assert canonical == b'{"a":{"c":3,"d":2},"b":1}\n'


def test_canonical_json_is_stable_across_key_insertion_order():
    # The hash is the report's address, so two equal documents built in
    # different orders must not produce different addresses.
    first = canonical_json({"a": 1, "b": [1, 2], "c": {"x": None}})
    second = canonical_json({"c": {"x": None}, "b": [1, 2], "a": 1})
    assert first == second
    assert content_hash(first) == content_hash(second)


def test_canonical_json_keeps_non_ascii_as_text():
    canonical = canonical_json({"name": "José — Ünicode"})
    assert "José — Ünicode" in canonical.decode("utf-8")
    assert b"\\u" not in canonical


def test_content_hash_is_the_sha256_of_those_exact_bytes():
    canonical = canonical_json({"a": 1})
    assert content_hash(canonical) == hashlib.sha256(canonical).hexdigest()


def test_report_path_sards_by_the_first_two_hex_characters():
    digest = "ab" + "c" * 62
    assert report_path(digest) == f"reports/ab/{digest}.json"


# --- The published document --------------------------------------------------

def test_document_carries_everything_needed_to_check_and_rerun_the_verdict():
    document = build_document(make_report())

    assert document["schema"] == att.DOCUMENT_SCHEMA
    assert document["report_id"] == "report-1"
    assert document["issued_at"] == "2026-09-20T14:30:00+00:00"
    assert document["repository"] == {
        "url": "https://github.com/octocat/portfolio", "owner": "octocat",
        "name": "portfolio", "commit_sha": "a3f9c1b", "default_branch": "main",
    }
    assert document["provenance"] == {
        "model": "grok-3", "prompt_version": "v2", "fetch_mode": "public",
        "fetched_at": "2026-09-20T14:29:00+00:00",
    }
    assert document["overall_trust_score"] == 84
    assert document["authorship_signals"] == {"copilot_trailers": 2}
    assert document["skills"][0]["evidence"][0]["start_line"] == 1


def test_document_publishes_no_internal_or_private_fields():
    serialised = canonical_json(build_document(make_report())).decode()
    # The log may be pushed to public mirrors: no email, and nothing internal.
    assert "octocat@example.test" not in serialised
    assert "raw_response" not in serialised
    assert "token_usage" not in serialised
    assert "github_username" in serialised


def test_document_orders_claimed_skills_first_then_alphabetically():
    report = make_report(skills=[
        skill("zod"), skill("CSS", claimed=False), skill("Ansible"),
    ])
    names = [entry["skill_name"] for entry in build_document(report)["skills"]]
    assert names == ["Ansible", "zod", "CSS"]


def test_naive_and_aware_timestamps_produce_the_same_bytes():
    aware = make_report()
    naive = make_report(created_at=CREATED_AT.replace(tzinfo=None))
    # SQLite returns naive datetimes where PostgreSQL returns aware ones; the
    # published bytes must not depend on which database produced them.
    assert canonical_json(build_document(aware)) == canonical_json(build_document(naive))


# --- The log -----------------------------------------------------------------

def git(repo, *args):
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def log(tmp_path):
    return AttestationLog(tmp_path / "attestations")


def test_append_commits_the_exact_bytes_that_were_hashed(log):
    record = log.append(build_document(make_report()))

    committed = (log.repo_path / record.path).read_bytes()
    assert committed == record.canonical_json
    assert hashlib.sha256(committed).hexdigest() == record.content_hash
    # The blob git actually stored, not just the working copy.
    blob = subprocess.run(["git", "show", f"{record.commit_sha}:{record.path}"],
                          cwd=log.repo_path, capture_output=True)
    assert hashlib.sha256(blob.stdout).hexdigest() == record.content_hash


def test_append_is_idempotent_for_identical_documents(log):
    document = build_document(make_report())
    first = log.append(document)
    second = log.append(document)

    # A retry after a partial failure must not fork the log or make an empty commit.
    assert first.commit_sha == second.commit_sha
    assert len(git(log.repo_path, "log", "--format=%H").splitlines()) == 2


def test_reanalysis_appends_a_second_commit_rather_than_amending(log):
    first = log.append(build_document(make_report()))
    second = log.append(build_document(make_report(overall_trust_score=91)))

    assert first.commit_sha != second.commit_sha
    assert first.content_hash != second.content_hash
    # The earlier report is still in the log, and still reachable.
    assert git(log.repo_path, "cat-file", "-t", first.commit_sha) == "commit"
    assert (log.repo_path / first.path).exists()
    assert git(log.repo_path, "rev-parse", f"{second.commit_sha}^") == first.commit_sha


def test_the_log_disables_end_of_line_translation(log):
    log.ensure_initialised()
    assert (log.repo_path / ".gitattributes").read_bytes() == b"reports/** -text\n"
    assert git(log.repo_path, "config", "core.autocrlf") == "false"


def test_commit_message_names_the_repository_and_the_hash(log):
    record = log.append(build_document(make_report()))
    message = git(log.repo_path, "log", "-1", "--format=%B")
    assert f"Attest report {record.content_hash[:12]} for octocat/portfolio" in message
    assert f"content-hash: {record.content_hash}" in message
    assert "repo-commit: a3f9c1b" in message


def test_git_failures_are_raised_as_attestation_errors(tmp_path):
    log = AttestationLog(tmp_path / "attestations", signing_key=tmp_path / "absent-key")
    with pytest.raises(AttestationError):
        log.append(build_document(make_report()))


# --- Signatures --------------------------------------------------------------

needs_ssh_keygen = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None, reason="ssh-keygen is not available"
)


@pytest.fixture
def signing_key(tmp_path):
    key = tmp_path / "attestation_key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q",
         "-C", att.SIGNER_IDENTITY],
        check=True, capture_output=True,
    )
    return key


@needs_ssh_keygen
def test_a_signed_commit_verifies_against_the_published_key(tmp_path, signing_key):
    log = AttestationLog(tmp_path / "attestations", signing_key=signing_key)
    record = log.append(build_document(make_report()))
    assert record.signed

    # Exactly what a third party does: take the key we publish at
    # /.well-known/skillchain-signing-key and check the commit against it.
    allowed = tmp_path / "allowed_signers"
    allowed.write_text(log.allowed_signers(), encoding="utf-8")
    verified = subprocess.run(
        ["git", "-c", f"gpg.ssh.allowedSignersFile={allowed}",
         "verify-commit", record.commit_sha],
        cwd=log.repo_path, capture_output=True, text=True,
    )
    assert verified.returncode == 0, verified.stderr
    assert "Good \"git\" signature" in verified.stderr


@needs_ssh_keygen
def test_every_commit_in_the_log_is_signed_including_the_root(tmp_path, signing_key):
    log = AttestationLog(tmp_path / "attestations", signing_key=signing_key)
    log.append(build_document(make_report()))

    allowed = tmp_path / "allowed_signers"
    allowed.write_text(log.allowed_signers(), encoding="utf-8")
    # %G? only resolves to 'G' once git has the allowed signers to check
    # against; without it even a valid SSH signature reads as unverifiable.
    statuses = subprocess.run(
        ["git", "-c", f"gpg.ssh.allowedSignersFile={allowed}", "log", "--format=%G?"],
        cwd=log.repo_path, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert len(statuses) == 2  # the root .gitattributes commit and the report
    assert set(statuses) == {"G"}


@needs_ssh_keygen
def test_allowed_signers_names_the_principal_the_commits_use(tmp_path, signing_key):
    log = AttestationLog(tmp_path / "attestations", signing_key=signing_key)
    line = log.allowed_signers()
    assert line.startswith(f"{att.SIGNER_IDENTITY} ssh-ed25519 ")
    assert line.endswith("\n")


def test_an_unsigned_log_reports_itself_as_unsigned(log):
    assert log.append(build_document(make_report())).signed is False
    assert log.allowed_signers() is None


# --- Mirrors -----------------------------------------------------------------

def test_push_mirrors_the_log_to_a_reachable_remote(tmp_path):
    mirror = tmp_path / "mirror.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(mirror)], check=True)
    log = AttestationLog(tmp_path / "attestations", remotes=[str(mirror)])
    record = log.append(build_document(make_report()))

    assert log.push() == [str(mirror)]
    # The commit is retrievable from the mirror alone, with no reference to us.
    assert git(mirror, "cat-file", "-t", record.commit_sha) == "commit"


def test_an_unreachable_mirror_does_not_fail_the_attestation(tmp_path):
    log = AttestationLog(tmp_path / "attestations",
                         remotes=[str(tmp_path / "nowhere.git")])
    record = log.append(build_document(make_report()))

    # The signed commit already exists locally; a mirror can be caught up later.
    assert log.push() == []
    assert record.commit_sha


def test_push_reports_only_the_mirrors_that_accepted(tmp_path):
    good = tmp_path / "good.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(good)], check=True)
    log = AttestationLog(tmp_path / "attestations",
                         remotes=[str(tmp_path / "bad.git"), str(good)])
    log.append(build_document(make_report()))
    assert log.push() == [str(good)]


# --- Configuration -----------------------------------------------------------

def test_attestation_is_off_unless_switched_on(monkeypatch):
    monkeypatch.delenv("ATTESTATION_ENABLED", raising=False)
    assert att.is_enabled() is False
    monkeypatch.setenv("ATTESTATION_ENABLED", "true")
    assert att.is_enabled() is True
    monkeypatch.setenv("ATTESTATION_ENABLED", "no")
    assert att.is_enabled() is False


def test_from_env_refuses_to_run_without_a_signing_key(monkeypatch, tmp_path):
    monkeypatch.setenv("ATTESTATION_REPO_PATH", str(tmp_path / "log"))
    monkeypatch.delenv("ATTESTATION_SIGNING_KEY_PATH", raising=False)
    # An attestation nobody can verify is worse than an honest failure.
    with pytest.raises(AttestationError, match="signing key"):
        from_env()


def test_from_env_reports_a_missing_key_file(monkeypatch, tmp_path):
    monkeypatch.setenv("ATTESTATION_REPO_PATH", str(tmp_path / "log"))
    monkeypatch.setenv("ATTESTATION_SIGNING_KEY_PATH", str(tmp_path / "absent"))
    with pytest.raises(AttestationError, match="does not exist"):
        from_env()


def test_from_env_reads_the_mirror_list(monkeypatch, tmp_path, signing_key):
    monkeypatch.setenv("ATTESTATION_REPO_PATH", str(tmp_path / "log"))
    monkeypatch.setenv("ATTESTATION_SIGNING_KEY_PATH", str(signing_key))
    monkeypatch.setenv("ATTESTATION_REMOTES", "https://a.example/log.git, https://b.example/log.git")
    assert from_env().remotes == ["https://a.example/log.git", "https://b.example/log.git"]


def test_verify_commands_reference_a_mirror_and_the_expected_hash(log):
    record = log.append(build_document(make_report()))
    record.mirrors = ["https://github.com/ecxof/skillchain-attestations.git"]
    commands = verify_commands(record)
    assert "git clone https://github.com/ecxof/skillchain-attestations.git" in commands[0]
    assert f"git verify-commit {record.commit_sha}" in commands[1]
    assert record.content_hash in commands[2]
