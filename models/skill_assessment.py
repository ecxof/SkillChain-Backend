import uuid

from sqlalchemy import (
    Boolean, CheckConstraint, Column, ForeignKey, Integer, JSON, String, Text,
    UniqueConstraint, true,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from db.database import Base, one_of

SKILL_LEVELS = ("beginner", "intermediate", "advanced")

class SkillAssessment(Base):
    """One skill's verdict within a report, with the evidence behind it."""

    __tablename__ = "skill_assessments"
    __table_args__ = (
        one_of("level", SKILL_LEVELS),
        CheckConstraint("confidence BETWEEN 0 AND 100", name="confidence_range"),
        UniqueConstraint("report_id", "skill_name"),
    )

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    report_id = Column(
        String, ForeignKey("analysis_reports.id", ondelete="CASCADE"), nullable=False
    )
    skill_name = Column(String, nullable=False)
    # False when the model found the skill without the submitter claiming it.
    claimed = Column(Boolean, nullable=False, default=True, server_default=true())
    verified = Column(Boolean, nullable=False)
    # Left unset when the skill could not be verified.
    level = Column(String, nullable=True)
    confidence = Column(Integer, nullable=True)
    reason = Column(Text, nullable=True)
    # List of {type, file, start_line, end_line, detail, source}. Every span is
    # checked against the snapshot's sampled file before it is stored, so a
    # citation always resolves to real lines.
    evidence = Column(JSON, nullable=False, default=list, server_default="[]")

    report = relationship("AnalysisReport", back_populates="skills")
