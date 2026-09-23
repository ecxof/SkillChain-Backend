"""Finish the Bitcoin anchors for reports whose proofs are still pending.

A report is stamped the moment it is issued, but the calendars cannot complete
the proof until Bitcoin has confirmed the block committing to it, which takes
hours. This job asks them again, stores whatever came back, and marks the
report anchored once a block height is known.

Run it on a schedule. Hourly is ample — confirmation is measured in hours, and
asking more often only adds load to free public infrastructure.

    python -m scripts.upgrade_timestamps

Nothing here is destructive. An upgrade that is still not ready leaves the
report exactly as it was, to be retried on the next run.
"""

import argparse
import logging
import sys
from datetime import datetime, timezone

from db.database import SessionLocal
from models.analysis_report import AnalysisReport
from services import attestation_service, timestamp_service

logger = logging.getLogger("upgrade_timestamps")

# A bound per run, so one invocation cannot sit on the calendars for hours.
DEFAULT_BATCH = 200


def pending_reports(session, limit: int) -> list[AnalysisReport]:
    """Reports that were stamped but are not anchored yet, oldest first."""
    return (
        session.query(AnalysisReport)
        .filter(AnalysisReport.timestamp_status == "pending")
        .filter(AnalysisReport.content_hash.isnot(None))
        .order_by(AnalysisReport.created_at)
        .limit(limit)
        .all()
    )


def upgrade_report(session, log, report) -> str:
    """Try to complete one report's proof. Returns what happened.

    'anchored' when Bitcoin has confirmed it, 'waiting' when the calendars are
    not ready, 'missing' when no proof was ever stored, 'failed' when the proof
    or the log could not be read.
    """
    try:
        proof = log.read_proof(report.content_hash)
    except OSError as exc:
        logger.warning("could not read the proof for report %s: %s", report.id, exc)
        return "failed"

    if proof is None:
        # Stamped according to the database, but nothing is in the log: the run
        # that stamped it failed between the two. Nothing to upgrade.
        logger.warning("report %s is pending but has no stored proof", report.id)
        report.timestamp_status = "failed"
        session.commit()
        return "missing"

    try:
        upgraded, anchor = timestamp_service.upgrade(proof)
    except timestamp_service.TimestampError as exc:
        logger.warning("could not upgrade the proof for report %s: %s", report.id, exc)
        report.timestamp_status = "failed"
        session.commit()
        return "failed"

    if anchor is None:
        # Normal: Bitcoin has not confirmed yet. Try again next run.
        return "waiting"

    try:
        log.append_proof(report.content_hash, upgraded)
    except attestation_service.AttestationError as exc:
        # The anchor is real even if the log could not record it; leave the
        # report pending so the next run retries rather than claiming success.
        logger.warning("could not commit the upgraded proof for report %s: %s", report.id, exc)
        return "failed"

    report.timestamp_status = "anchored"
    report.anchored_at = datetime.now(timezone.utc)
    report.bitcoin_block_height = anchor.block_height
    session.commit()
    logger.info("report %s anchored in Bitcoin block %s", report.id, anchor.block_height)
    return "anchored"


def upgrade_pending(session, log, *, limit: int = DEFAULT_BATCH) -> dict[str, int]:
    """Upgrade every pending proof, returning a count of each outcome."""
    counts = {"anchored": 0, "waiting": 0, "missing": 0, "failed": 0}
    reports = pending_reports(session, limit)
    for report in reports:
        counts[upgrade_report(session, log, report)] += 1
    if counts["anchored"]:
        # Mirrors should carry the completed proofs too, not just the reports.
        log.push()
    return counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH,
                        help=f"maximum reports to attempt (default {DEFAULT_BATCH})")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not timestamp_service.is_enabled():
        logger.info("OTS_ENABLED is off; there is nothing to upgrade")
        return 0

    try:
        log = attestation_service.from_env()
    except attestation_service.AttestationError as exc:
        logger.error("the attestation log is not configured: %s", exc)
        return 1

    session = SessionLocal()
    try:
        counts = upgrade_pending(session, log, limit=args.limit)
    finally:
        session.close()

    logger.info("anchored %(anchored)s, still waiting %(waiting)s, "
                "no proof %(missing)s, failed %(failed)s", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
