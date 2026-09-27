"""The one walk that joins every layer: submit, analyse, publish, verify.

Each stage has thorough tests of its own, and each of those stubs its
neighbours, so nothing until now drove a submission all the way to a proof a
stranger could check. This file stubs only what reaches the network — GitHub
and the model — and lets the rest run for real: an HTTP submission, the
pipeline, a signed git repository on disk, and the published proof read back
over HTTP and verified with git rather than with anything this suite owns.
"""

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from services import analysis_service, attestation_service, timestamp_service  # noqa: E402
from services.ai_service import LLMResponse  # noqa: E402
from services.attestation_service import AttestationLog  # noqa: E402

HEAD = "a3f9c1b" + "0" * 33


def repo_data():
    """What fetching the submitted repository would have returned."""
    return SimpleNamespace(snapshot_fields=lambda: {
        "commit_sha": HEAD, "default_branch": "main",
        "languages": {"Python": 900}, "topics": ["fastapi"], "stars": 3, "forks": 0,
        "commit_count": 214, "user_commit_count": 190,
        "readme_content": "# Portfolio",
        "file_tree": {"truncated": False, "entry_count": 1,
                      "entries": [{"path": "main.py", "size": 300}]},
        "sampled_files": {"main.py": "def main():\n    return 1\n"},
        "commit_history": [], "contributor_stats": None, "fetch_mode": "public",
        "fetched_at": datetime.now(timezone.utc),
    })


REPLY = json.dumps({
    "overall_trust_score": 84,
    "summary": "A small FastAPI service.",
    "skills": [{
        "skill_name": "Python", "claimed": True, "verified": True,
        "level": "advanced", "confidence": 88,
        "reason": "Type hints throughout.",
        # Cites real lines of the file the fetch served, so the span validator
        # resolves it rather than rejecting the citation.
        "evidence": [{"type": "source", "file": "main.py", "start_line": 1,
                      "end_line": 2, "detail": "The entry point."}],
    }],
})


class StubModel:
    """Answers with a fixed reply, so nothing reaches a model provider.

    Stubbing here rather than at analyze_snapshot is deliberate: prompt
    building, reply parsing and span validation are not network calls, and
    skipping them is how a walk like this comes to assert on a report shape
    the API would refuse to serve.
    """

    model_name = "grok-3"

    async def complete(self, prompt):
        return LLMResponse(text=REPLY, model="grok-3",
                           token_usage={"prompt_tokens": 100, "completion_tokens": 20,
                                        "total_tokens": 120})


@pytest.fixture
def published(monkeypatch, db_session, tmp_path, signing_key):
    """Let the route's background task run the real pipeline on this database.

    run_pipeline takes its session factory as a default argument bound when
    the function was defined, so patching SessionLocal afterwards cannot reach
    it. The route looks the function up on the module at call time, and that is
    the seam: wrap the real pipeline and hand it the fixture's engine and a
    signed log on disk.

    Returns the log, since verifying what came out of it is the point.
    """
    log = AttestationLog(tmp_path / "attestations", signing_key=signing_key)
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False)
    real_pipeline = analysis_service.run_pipeline

    async def fake_fetch(owner, name, **kwargs):
        return repo_data()

    async def on_the_test_database(project_id, **kwargs):
        return await real_pipeline(project_id, session_factory=factory, log=log,
                                   provider=StubModel())

    monkeypatch.setattr(analysis_service, "fetch_repository", fake_fetch)
    monkeypatch.setattr(analysis_service, "run_pipeline", on_the_test_database)
    monkeypatch.setattr(attestation_service, "is_enabled", lambda: True)
    # Nothing in a test should wait on a Bitcoin calendar server.
    monkeypatch.setattr(timestamp_service, "is_enabled", lambda: False)
    # Both the attestation endpoint and the well-known key resolve through this.
    monkeypatch.setattr(attestation_service, "from_env", lambda: log)
    return log


def submit(client, auth_headers):
    """Submit a public project the way a client would."""
    return client.post("/projects", headers=auth_headers, json={
        "title": "Portfolio",
        "github_repo_url": "https://github.com/ada/portfolio",
        "skills_claimed": ["Python"],
        "role_in_project": "Sole developer",
        "visibility": "public",
    })


def finished_report(client, db_session, auth_headers):
    """Submit, let the pipeline run, and return the report it produced."""
    response = submit(client, auth_headers)
    assert response.status_code == 202
    project_id = response.json()["id"]

    # TestClient runs background tasks for real before returning, and the
    # pipeline wrote through a session of its own; this one still holds the
    # 'pending' row it loaded while serving the response.
    db_session.expire_all()

    project = client.get(f"/projects/{project_id}", headers=auth_headers).json()
    assert project["status"] == "completed", project.get("error_message")
    return project["latest_report"]


def test_a_submission_becomes_a_published_report(
    client, db_session, user, auth_headers, published
):
    report = finished_report(client, db_session, auth_headers)

    assert report["overall_trust_score"] == 84
    assert [skill["skill_name"] for skill in report["skills"]] == ["Python"]
    # Verified only survives if the citation resolved: _assess_skills drops the
    # claim to unverified when the span validator rejects its evidence.
    assert report["skills"][0]["verified"] is True
    assert report["skills"][0]["evidence"][0]["file"] == "main.py"
    # Addressed and in the log, not merely analysed.
    assert report["attestation"]["status"] == "committed"
    assert report["attestation"]["content_hash"]


def test_the_published_proof_verifies_without_trusting_the_api(
    client, db_session, user, auth_headers, published, tmp_path
):
    report_id = finished_report(client, db_session, auth_headers)["id"]

    # Everything from here uses only what an anonymous reader can fetch.
    proof = client.get(f"/public/reports/{report_id}/attestation").json()
    key = client.get("/.well-known/skillchain-signing-key").text

    allowed = tmp_path / "allowed_signers"
    allowed.write_text(key, encoding="utf-8")
    verified = subprocess.run(
        ["git", "-c", f"gpg.ssh.allowedSignersFile={allowed}",
         "verify-commit", proof["commit_sha"]],
        cwd=published.repo_path, capture_output=True, text=True,
    )
    assert verified.returncode == 0, verified.stderr

    committed = subprocess.run(
        ["git", "show", f"{proof['commit_sha']}:"
                        f"{attestation_service.report_path(proof['content_hash'])}"],
        cwd=published.repo_path, capture_output=True, check=True,
    ).stdout

    # Three separate claims, and the claim only holds if all three do: the
    # commit was signed by the key we publish, it carries exactly the bytes the
    # API served, and those bytes are what the published hash addresses.
    assert committed == proof["canonical_json"].encode("utf-8")
    assert hashlib.sha256(committed).hexdigest() == proof["content_hash"]


def test_the_report_a_recruiter_reads_is_the_one_that_was_signed(
    client, db_session, user, auth_headers, published
):
    report_id = finished_report(client, db_session, auth_headers)["id"]

    public = client.get(f"/public/reports/{report_id}").json()
    proof = client.get(f"/public/reports/{report_id}/attestation").json()

    # The public view and the attested document have to agree, or the proof is
    # for a different report than the one on display.
    assert proof["canonical_json"] is not None
    assert f'"summary":"{public["summary"]}"' in proof["canonical_json"]
    assert f'"overall_trust_score":{public["overall_trust_score"]}' in proof["canonical_json"]
