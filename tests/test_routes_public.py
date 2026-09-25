import base64
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite://")

from services import attestation_service  # noqa: E402
from tests.factories import completed_project, make_project, make_report  # noqa: E402


def publish(db_session, user, slug="ada"):
    user.profile_slug = slug
    user.profile_is_public = True
    db_session.commit()
    return user


# --- Profiles -----------------------------------------------------------------

def test_a_published_profile_is_served_without_a_token(client, db_session, user):
    publish(db_session, user)
    completed_project(db_session, user, visibility="public")

    body = client.get("/public/profiles/ada").json()

    assert body["display_name"] == "Ada Lovelace"
    assert [skill["skill_name"] for skill in body["skills"]] == ["Python"]
    assert body["skills"][0]["highest_level"] == "advanced"


def test_an_unpublished_profile_is_indistinguishable_from_no_profile(
    client, db_session, user
):
    user.profile_slug = "ada"
    user.profile_is_public = False
    db_session.commit()

    assert client.get("/public/profiles/ada").status_code == 404
    # Same answer for a slug nobody ever claimed.
    assert client.get("/public/profiles/nobody").status_code == 404


def test_a_private_project_never_reaches_a_public_profile(client, db_session, user):
    publish(db_session, user)
    completed_project(db_session, user, visibility="private")

    body = client.get("/public/profiles/ada").json()

    # Both gates must be open: the profile is published, but the project is not.
    assert body["skills"] == []


def test_an_unverified_skill_is_not_claimed_on_the_profile(client, db_session, user):
    publish(db_session, user)
    project = make_project(db_session, user, status="completed", visibility="public")
    make_report(db_session, project, verified=False)

    body = client.get("/public/profiles/ada").json()

    assert body["skills"] == []


def test_the_profile_links_to_the_reports_behind_each_skill(client, db_session, user):
    publish(db_session, user)
    _, report = completed_project(db_session, user, visibility="public")

    body = client.get("/public/profiles/ada").json()

    # The evidence has to be reachable, or the claim is just an assertion.
    assert body["skills"][0]["report_ids"] == [report.id]
    assert client.get(f"/public/reports/{report.id}").status_code == 200


# --- Reports ------------------------------------------------------------------

def test_a_report_on_a_public_project_is_readable_by_anyone(client, db_session, user):
    _, report = completed_project(db_session, user, visibility="public")

    body = client.get(f"/public/reports/{report.id}").json()

    assert body["id"] == report.id
    assert body["overall_trust_score"] == 82
    assert body["skills"][0]["evidence"][0]["file"] == "services/analysis_service.py"


def test_a_report_on_a_private_project_is_reported_as_missing(client, db_session, user):
    _, report = completed_project(db_session, user, visibility="private")

    # Not 403: that would confirm the report exists.
    assert client.get(f"/public/reports/{report.id}").status_code == 404


def test_a_report_on_a_project_still_running_is_not_served(client, db_session, user):
    project = make_project(db_session, user, status="analyzing", visibility="public")
    report = make_report(db_session, project)

    assert client.get(f"/public/reports/{report.id}").status_code == 404


def test_a_superseded_report_stays_readable(client, db_session, user):
    project = make_project(db_session, user, status="completed", visibility="public")
    older = make_report(db_session, project,
                        created_at=datetime.now(timezone.utc) - timedelta(days=2))
    make_report(db_session, project)

    # Re-analysis appends rather than replaces. If only the newest were served,
    # a developer could quietly retire a verdict they had already shared.
    assert client.get(f"/public/reports/{older.id}").status_code == 200


def test_an_unknown_report_id_is_a_plain_404(client):
    assert client.get("/public/reports/not-a-real-id").status_code == 404


def test_the_public_report_withholds_the_raw_model_response(client, db_session, user):
    _, report = completed_project(db_session, user, visibility="public")

    body = client.get(f"/public/reports/{report.id}").json()

    assert "raw_response" not in body
    assert "token_usage" not in body


# --- The signing key ----------------------------------------------------------

def test_the_signing_key_is_published_where_a_verifier_would_look(client, monkeypatch):
    class Log:
        def allowed_signers(self):
            return "attestation@skillchain ssh-ed25519 AAAAC3Nz...\n"

    monkeypatch.setattr(attestation_service, "from_env", Log)

    response = client.get("/.well-known/skillchain-signing-key")

    assert response.status_code == 200
    assert response.text.startswith("attestation@skillchain ssh-ed25519")
    assert response.headers["content-type"].startswith("text/plain")


def test_a_deployment_with_no_attestation_says_so_rather_than_failing(client, monkeypatch):
    def unconfigured():
        raise attestation_service.AttestationError("ATTESTATION_REPO_PATH is not set")

    monkeypatch.setattr(attestation_service, "from_env", unconfigured)

    # 404, not 500: nothing is broken and the caller did nothing wrong.
    assert client.get("/.well-known/skillchain-signing-key").status_code == 404


# --- Attestation --------------------------------------------------------------

MIRRORS = [
    "https://github.com/skillchain/attestations.git",
    "https://gitlab.com/skillchain/attestations.git",
]


class FakeLog:
    """A configured attestation repository, minus the git repository."""

    def __init__(self, remotes=(), proof=None):
        self.remotes = list(remotes)
        self.proof = proof

    def read_proof(self, digest):
        return self.proof


def configure(monkeypatch, log):
    monkeypatch.setattr(attestation_service, "from_env", lambda: log)


def attest(db_session, report, *, status="mirrored", commit_sha="9f3c1ab"):
    """Stamp a report with the hash it would have been published under.

    Computed from the row as the database returns it, because the endpoint
    recomputes it the same way and withholds bytes that disagree. Building the
    document from values still in memory would hash tz-aware datetimes that
    SQLite gives back naive.
    """
    db_session.expire_all()
    canonical = attestation_service.canonical_json(
        attestation_service.build_document(report)
    )
    report.content_hash = attestation_service.content_hash(canonical)
    report.attestation_status = status
    report.attestation_commit_sha = commit_sha
    report.attested_at = datetime.now(timezone.utc)
    db_session.commit()
    return report


def test_a_mirrored_report_says_where_the_log_can_be_cloned_from(
    client, db_session, user, monkeypatch
):
    _, report = completed_project(db_session, user, visibility="public")
    attest(db_session, report)
    configure(monkeypatch, FakeLog(remotes=MIRRORS))

    body = client.get(f"/public/reports/{report.id}/attestation").json()

    assert body["mirrors"] == MIRRORS
    assert body["verify_commands"][0] == f"git clone {MIRRORS[0]} skillchain-attestations"
    assert body["commit_sha"] == "9f3c1ab"


def test_the_published_bytes_are_served_and_still_hash_to_the_stored_digest(
    client, db_session, user, monkeypatch
):
    _, report = completed_project(db_session, user, visibility="public")
    attest(db_session, report)
    configure(monkeypatch, FakeLog(remotes=MIRRORS))

    body = client.get(f"/public/reports/{report.id}/attestation").json()

    # The point of the endpoint: a reader can redo the hash themselves.
    recomputed = attestation_service.content_hash(body["canonical_json"].encode("utf-8"))
    assert recomputed == body["content_hash"]


def test_a_report_only_committed_locally_advertises_no_mirror(
    client, db_session, user, monkeypatch
):
    _, report = completed_project(db_session, user, visibility="public")
    attest(db_session, report, status="committed")
    configure(monkeypatch, FakeLog(remotes=MIRRORS))

    body = client.get(f"/public/reports/{report.id}/attestation").json()

    # The signed commit exists but no mirror has it, and naming one anyway
    # would send a reader somewhere the proof is not.
    assert body["mirrors"] == []
    assert "<attestation-repo-url>" in body["verify_commands"][0]


def test_a_deployment_with_no_attestation_still_serves_what_it_has(
    client, db_session, user, monkeypatch
):
    _, report = completed_project(db_session, user, visibility="public")
    attest(db_session, report)

    def unconfigured():
        raise attestation_service.AttestationError("ATTESTATION_REPO_PATH is not set")

    monkeypatch.setattr(attestation_service, "from_env", unconfigured)

    response = client.get(f"/public/reports/{report.id}/attestation")

    assert response.status_code == 200
    body = response.json()
    assert body["mirrors"] == []
    assert body["ots_proof"] is None
    # The canonical bytes are rebuilt from the row, not read from the log, so
    # they survive a deployment that has no log at all.
    assert body["canonical_json"] is not None


def test_a_bitcoin_proof_is_served_in_a_form_json_can_carry(
    client, db_session, user, monkeypatch
):
    _, report = completed_project(db_session, user, visibility="public")
    attest(db_session, report)
    configure(monkeypatch, FakeLog(remotes=MIRRORS, proof=b"\x00OpenTimestamps\xff"))

    body = client.get(f"/public/reports/{report.id}/attestation").json()

    assert base64.b64decode(body["ots_proof"]) == b"\x00OpenTimestamps\xff"


def test_anchoring_a_report_later_does_not_invalidate_its_proof(
    client, db_session, user, monkeypatch
):
    _, report = completed_project(db_session, user, visibility="public")
    attest(db_session, report)
    report.timestamp_status = "anchored"
    report.anchored_at = datetime.now(timezone.utc)
    report.bitcoin_block_height = 912345
    db_session.commit()
    configure(monkeypatch, FakeLog(remotes=MIRRORS))

    body = client.get(f"/public/reports/{report.id}/attestation").json()

    # The upgrader writes these columns long after the report was hashed. If
    # any of them were part of the canonical document, every report would stop
    # verifying at the moment its proof got better.
    assert body["canonical_json"] is not None
    assert body["bitcoin_block_height"] == 912345


def test_a_report_that_no_longer_matches_its_hash_withholds_the_proof(
    client, db_session, user, monkeypatch
):
    _, report = completed_project(db_session, user, visibility="public")
    attest(db_session, report)
    report.summary = "Quietly reworded after publication."
    db_session.commit()
    configure(monkeypatch, FakeLog(remotes=MIRRORS))

    body = client.get(f"/public/reports/{report.id}/attestation").json()

    # Serving these bytes under the published hash would be a false proof.
    assert body["canonical_json"] is None
    assert body["verify_commands"] == []
    # The hash is still reported, so a reader can see that it no longer holds.
    assert body["content_hash"] is not None


def test_the_attestation_of_a_private_project_is_reported_as_missing(
    client, db_session, user, monkeypatch
):
    _, report = completed_project(db_session, user, visibility="private")
    attest(db_session, report)
    configure(monkeypatch, FakeLog(remotes=MIRRORS))

    # Same gate as the report itself, for the same reason.
    assert client.get(f"/public/reports/{report.id}/attestation").status_code == 404
