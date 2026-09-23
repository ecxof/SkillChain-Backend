import hashlib
import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")

from opentimestamps.core.notary import (  # noqa: E402
    BitcoinBlockHeaderAttestation, PendingAttestation,
)
from opentimestamps.core.timestamp import Timestamp  # noqa: E402

from services import timestamp_service as ts  # noqa: E402
from services.timestamp_service import (  # noqa: E402
    BitcoinAnchor, TimestampError, anchor_of, digest_of, stamp, upgrade,
)

DIGEST = hashlib.sha256(b"a skillchain report").digest()
CALENDARS = ("https://alice.example", "https://bob.example")


class FakeCalendar:
    """Stands in for a public calendar; records what it was actually shown."""

    submitted = []
    requested = []
    # commitment -> attestation to hand back from get_timestamp
    completions = {}
    unreachable = set()

    def __init__(self, url):
        self.url = url

    def submit(self, digest, timeout=None):
        if self.url in self.unreachable:
            raise ConnectionError("calendar down")
        FakeCalendar.submitted.append((self.url, digest))
        result = Timestamp(digest)
        result.attestations.add(PendingAttestation(self.url))
        return result

    def get_timestamp(self, commitment, timeout=None):
        if self.url in self.unreachable:
            raise ConnectionError("calendar down")
        FakeCalendar.requested.append((self.url, commitment))
        height = FakeCalendar.completions.get(self.url)
        if height is None:
            raise Exception("not yet confirmed by Bitcoin")
        result = Timestamp(commitment)
        result.attestations.add(BitcoinBlockHeaderAttestation(height))
        return result


@pytest.fixture(autouse=True)
def reset_calendar():
    FakeCalendar.submitted = []
    FakeCalendar.requested = []
    FakeCalendar.completions = {}
    FakeCalendar.unreachable = set()
    yield


def make_pending(digest=DIGEST, calendars=CALENDARS):
    return stamp(digest, calendars=calendars, calendar_factory=FakeCalendar)


# --- Stamping ----------------------------------------------------------------

def test_stamp_returns_a_proof_committing_to_the_content_hash():
    proof = make_pending()
    assert digest_of(proof) == DIGEST
    # Pending: the calendars have it, Bitcoin has not confirmed a block yet.
    assert anchor_of(proof) is None


def test_stamp_submits_to_every_configured_calendar():
    make_pending()
    assert [url for url, _ in FakeCalendar.submitted] == list(CALENDARS)


def test_calendars_never_see_the_content_hash_itself():
    make_pending()
    # A nonce is appended and rehashed before submission, so public calendar
    # operators cannot correlate a submission with a published report.
    for _, submitted in FakeCalendar.submitted:
        assert submitted != DIGEST
    assert len({submitted for _, submitted in FakeCalendar.submitted}) == 1


def test_stamp_survives_a_calendar_being_down():
    FakeCalendar.unreachable = {"https://alice.example"}
    proof = make_pending()
    # One calendar answering is enough for a usable proof.
    assert digest_of(proof) == DIGEST
    assert [url for url, _ in FakeCalendar.submitted] == ["https://bob.example"]


def test_stamp_fails_when_no_calendar_accepts():
    FakeCalendar.unreachable = set(CALENDARS)
    with pytest.raises(TimestampError, match="no OpenTimestamps calendar"):
        make_pending()


def test_stamp_rejects_anything_that_is_not_a_sha256_digest():
    with pytest.raises(TimestampError, match="32 bytes"):
        stamp(b"too short", calendars=CALENDARS, calendar_factory=FakeCalendar)


# --- Upgrading ---------------------------------------------------------------

def test_upgrade_leaves_a_proof_pending_until_bitcoin_confirms():
    proof = make_pending()
    upgraded, anchor = upgrade(proof, calendars=CALENDARS, calendar_factory=FakeCalendar)

    # Not an error: the caller leaves it pending and tries again later.
    assert anchor is None
    assert upgraded == proof
    # Attestations are held in a set, so every calendar is asked but the order
    # they are asked in is not fixed.
    assert {url for url, _ in FakeCalendar.requested} == set(CALENDARS)


def test_upgrade_anchors_the_proof_once_a_calendar_completes_it():
    proof = make_pending()
    FakeCalendar.completions = {"https://alice.example": 870_123}

    upgraded, anchor = upgrade(proof, calendars=CALENDARS, calendar_factory=FakeCalendar)

    assert anchor == BitcoinAnchor(block_height=870_123)
    assert upgraded != proof
    # The upgraded proof still commits to the same report.
    assert digest_of(upgraded) == DIGEST
    assert anchor_of(upgraded) == BitcoinAnchor(block_height=870_123)


def test_the_earliest_block_wins_when_several_calendars_complete():
    proof = make_pending()
    FakeCalendar.completions = {
        "https://alice.example": 870_500, "https://bob.example": 870_123,
    }
    _, anchor = upgrade(proof, calendars=CALENDARS, calendar_factory=FakeCalendar)
    # "Existed by" is the honest claim, so the earliest confirmation is the answer.
    assert anchor == BitcoinAnchor(block_height=870_123)


def test_upgrade_never_contacts_a_calendar_that_is_not_configured():
    proof = make_pending(calendars=("https://evil.example",))
    FakeCalendar.requested = []

    _, anchor = upgrade(proof, calendars=CALENDARS, calendar_factory=FakeCalendar)

    # The URI comes out of a stored file; a tampered proof must not be able to
    # point the upgrade job at an arbitrary host.
    assert FakeCalendar.requested == []
    assert anchor is None


def test_an_unreadable_proof_is_reported_rather_than_crashing():
    with pytest.raises(TimestampError, match="not readable"):
        anchor_of(b"this is not an .ots file")


# --- Configuration -----------------------------------------------------------

def test_timestamping_is_off_unless_switched_on(monkeypatch):
    monkeypatch.delenv("OTS_ENABLED", raising=False)
    assert ts.is_enabled() is False
    monkeypatch.setenv("OTS_ENABLED", "true")
    assert ts.is_enabled() is True


def test_the_public_calendars_are_used_unless_overridden(monkeypatch):
    monkeypatch.delenv("OTS_CALENDARS", raising=False)
    assert ts.configured_calendars() == ts.DEFAULT_CALENDARS
    monkeypatch.setenv("OTS_CALENDARS", "https://one.example, https://two.example")
    assert ts.configured_calendars() == ("https://one.example", "https://two.example")


# --- Storage in the attestation log ------------------------------------------

def test_proofs_are_committed_beside_their_reports(tmp_path):
    from services.attestation_service import AttestationLog, proof_path

    digest = hashlib.sha256(b"report").hexdigest()
    log = AttestationLog(tmp_path / "attestations")
    proof = make_pending()

    commit_sha = log.append_proof(digest, proof)

    stored = log.repo_path / proof_path(digest)
    assert stored.read_bytes() == proof
    assert proof_path(digest) == f"reports/{digest[:2]}/{digest}.ots"
    assert commit_sha


def test_an_upgraded_proof_is_appended_rather_than_replacing_the_pending_one(tmp_path):
    from services.attestation_service import AttestationLog

    digest = hashlib.sha256(b"report").hexdigest()
    log = AttestationLog(tmp_path / "attestations")
    pending = make_pending()
    first = log.append_proof(digest, pending)

    FakeCalendar.completions = {"https://alice.example": 870_123}
    upgraded, _ = upgrade(pending, calendars=CALENDARS, calendar_factory=FakeCalendar)
    second = log.append_proof(digest, upgraded)

    # The log records the upgrade instead of erasing the weaker earlier proof.
    assert first != second
    assert log.append_proof(digest, upgraded) == second  # idempotent
