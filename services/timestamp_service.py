"""Anchoring a report's content hash in Bitcoin, with no wallet and no fees.

OpenTimestamps calendars aggregate thousands of digests into a Merkle tree and
commit one root to Bitcoin. Only a hash ever leaves this machine: no wallet, no
gas, no contract, no RPC endpoint, no API key. The proof that comes back is a
Merkle path to a Bitcoin block header, verifiable against any Bitcoin node by
anyone, including against us.

Anchoring is a two-step affair. :func:`stamp` returns a *pending* proof in
milliseconds — the calendars have the digest but Bitcoin has not confirmed the
block yet. Hours later :func:`upgrade` asks the same calendars to complete the
proof. This is why ``timestamp_status`` has both a ``pending`` and an
``anchored`` state, and why the signature layer is separate: git proves who
issued a report immediately, Bitcoin proves when, eventually.

The calls block on network I/O, so async callers should reach them through
``asyncio.to_thread``.
"""

import logging
import os
import secrets
from dataclasses import dataclass

from opentimestamps.calendar import RemoteCalendar
from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
from opentimestamps.core.serialize import (
    BytesDeserializationContext, BytesSerializationContext, DeserializationError,
)
from opentimestamps.core.timestamp import (
    DetachedTimestampFile, OpAppend, OpSHA256, Timestamp,
)

logger = logging.getLogger(__name__)

# The public calendars the reference client uses. They are independent
# operators, and one proof can carry attestations from several, so a single
# calendar disappearing does not strand a report.
DEFAULT_CALENDARS = (
    "https://alice.btc.calendar.opentimestamps.org",
    "https://bob.btc.calendar.opentimestamps.org",
    "https://finney.calendar.eternitywall.com",
)

CALENDAR_TIMEOUT = 10.0
# A random nonce is appended before hashing, so the calendar commits to a value
# it cannot link back to the report. Calendars are public infrastructure and
# see every digest submitted to them.
NONCE_BYTES = 16


class TimestampError(RuntimeError):
    """The digest could not be stamped or a proof could not be upgraded.

    Recorded as ``timestamp_status='failed'``. The report still saves, still
    serves, and keeps its git signature; only the Bitcoin anchor is missing.
    """


@dataclass(frozen=True)
class BitcoinAnchor:
    """A confirmed anchor: the block whose header commits to the proof."""

    block_height: int


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def is_enabled() -> bool:
    """Whether reports should be timestamped. Off by default: it needs network."""
    return _flag("OTS_ENABLED")


def configured_calendars() -> tuple[str, ...]:
    raw = os.getenv("OTS_CALENDARS")
    if not raw:
        return DEFAULT_CALENDARS
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _serialize(detached: DetachedTimestampFile) -> bytes:
    context = BytesSerializationContext()
    detached.serialize(context)
    return context.getbytes()


def _deserialize(proof: bytes) -> DetachedTimestampFile:
    try:
        return DetachedTimestampFile.deserialize(BytesDeserializationContext(proof))
    except (DeserializationError, ValueError, TypeError) as exc:
        raise TimestampError(f"the stored .ots proof is not readable: {exc}") from exc


def stamp(digest: bytes, *, calendars=None, calendar_factory=RemoteCalendar) -> bytes:
    """Submit a content hash to the calendars and return a pending .ots proof.

    ``digest`` is the raw 32 bytes of the report's sha256 content hash. The
    proof is returned even if only one calendar answered; it fails only when
    none did, since a proof with no attestation would anchor nothing.
    """
    if len(digest) != 32:
        raise TimestampError("a content hash must be 32 bytes of sha256")

    detached = DetachedTimestampFile(OpSHA256(), Timestamp(digest))
    # Commit to digest+nonce rather than the digest itself, then hash again:
    # the calendar learns a value it cannot correlate with the published report.
    nonced = detached.timestamp.ops.add(OpAppend(secrets.token_bytes(NONCE_BYTES)))
    merkle_root = nonced.ops.add(OpSHA256())

    accepted, failures = 0, []
    for url in calendars or configured_calendars():
        try:
            response = calendar_factory(url).submit(merkle_root.msg, timeout=CALENDAR_TIMEOUT)
            merkle_root.merge(response)
            accepted += 1
        except Exception as exc:  # any calendar transport or protocol failure
            failures.append(f"{url}: {exc}")
            logger.warning("OpenTimestamps calendar %s refused a submission: %s", url, exc)

    if not accepted:
        raise TimestampError("no OpenTimestamps calendar accepted the digest: "
                             + "; ".join(failures))
    return _serialize(detached)


def anchor_of(proof: bytes) -> BitcoinAnchor | None:
    """The confirmed Bitcoin anchor in a proof, or None while it is still pending.

    A proof can carry several attestations once more than one calendar has
    completed it; the earliest block is the honest answer to "when did this
    exist by".
    """
    detached = _deserialize(proof)
    heights = [
        attestation.height
        for _, attestation in detached.timestamp.all_attestations()
        if isinstance(attestation, BitcoinBlockHeaderAttestation)
    ]
    return BitcoinAnchor(block_height=min(heights)) if heights else None


def digest_of(proof: bytes) -> bytes:
    """The content hash a proof commits to, for checking it against a report."""
    return _deserialize(proof).file_digest


def _upgrade_branch(timestamp: Timestamp, allowed: set[str], calendar_factory) -> bool:
    """Ask each pending calendar to complete this branch. True if anything merged."""
    merged = False
    for sub_timestamp in list(timestamp.ops.values()):
        merged |= _upgrade_branch(sub_timestamp, allowed, calendar_factory)

    for attestation in list(timestamp.attestations):
        if not isinstance(attestation, PendingAttestation):
            continue
        uri = attestation.uri
        if isinstance(uri, bytes):
            uri = uri.decode("utf-8", "replace")
        # The URI comes out of a stored file. Only ever call a calendar we
        # already trust, so a tampered proof cannot point us at an arbitrary host.
        if uri not in allowed:
            logger.warning("ignoring a pending attestation for unconfigured calendar %s", uri)
            continue
        try:
            completed = calendar_factory(uri).get_timestamp(
                timestamp.msg, timeout=CALENDAR_TIMEOUT
            )
        except Exception as exc:
            # Still waiting on a Bitcoin block is the normal case, and it
            # surfaces here as an error from the calendar.
            logger.debug("calendar %s has not completed this timestamp yet: %s", uri, exc)
            continue
        if completed is not None:
            timestamp.merge(completed)
            merged = True
    return merged


def upgrade(proof: bytes, *, calendars=None,
            calendar_factory=RemoteCalendar) -> tuple[bytes, BitcoinAnchor | None]:
    """Complete a pending proof if Bitcoin has confirmed it.

    Returns the proof to store — the upgraded one when anything merged, the
    original otherwise — and the anchor if there now is one. A proof that is
    simply not ready yet is not an error: the caller leaves it pending and
    tries again on the next run.
    """
    detached = _deserialize(proof)
    allowed = set(calendars or configured_calendars())
    merged = _upgrade_branch(detached.timestamp, allowed, calendar_factory)
    upgraded = _serialize(detached) if merged else proof
    return upgraded, anchor_of(upgraded)
