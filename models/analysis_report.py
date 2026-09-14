import uuid

from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, Integer, JSON, String, Text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from db.database import Base, one_of

# Progress of the report through the signed, append-only git log.
ATTESTATION_STATUSES = ("pending", "committed", "mirrored", "failed")
# Progress of the OpenTimestamps proof: submitted to a calendar, then anchored
# once Bitcoin confirms, which takes hours rather than seconds.
TIMESTAMP_STATUSES = ("none", "pending", "anchored", "failed")

class AnalysisReport(Base):
    """One model's verdict on one snapshot, with everything needed to re-run it."""

    __tablename__ = "analysis_reports"
    __table_args__ = (
        one_of("attestation_status", ATTESTATION_STATUSES),
        one_of("timestamp_status", TIMESTAMP_STATUSES),
        CheckConstraint(
            "overall_trust_score BETWEEN 0 AND 100", name="overall_trust_score_range"
        ),
    )

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # One report per snapshot; re-analysis takes a fresh snapshot rather than
    # overwriting, so the history of what was said stays intact.
    repo_snapshot_id = Column(
        String, ForeignKey("repo_snapshots.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    # Provenance: which model and which prompt produced this verdict.
    model_name = Column(String, nullable=False)
    prompt_version = Column(String, nullable=False)
    overall_trust_score = Column(Integer, nullable=True)
    summary = Column(Text, nullable=True)
    # Deterministic development-pattern facts derived from the commit history.
    # Never an AI-probability score.
    authorship_signals = Column(JSON, nullable=True)
    raw_response = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    token_usage = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # --- Attestation: canonical JSON committed to the signed git log ---
    # sha256 of the canonical JSON; the report's content address.
    content_hash = Column(String(64), nullable=True, index=True)
    attestation_commit_sha = Column(String(64), nullable=True)
    attestation_status = Column(
        String, nullable=False, default="pending", server_default="pending"
    )
    attested_at = Column(DateTime(timezone=True), nullable=True)

    # --- Timestamp: OpenTimestamps proof anchoring content_hash in Bitcoin ---
    timestamp_status = Column(String, nullable=False, default="none", server_default="none")
    anchored_at = Column(DateTime(timezone=True), nullable=True)
    bitcoin_block_height = Column(Integer, nullable=True)

    snapshot = relationship("RepoSnapshot", back_populates="report")
    skills = relationship(
        "SkillAssessment",
        back_populates="report",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
