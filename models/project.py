import uuid

from sqlalchemy import Column, DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from db.database import Base, one_of

# Set by analysis_service as the pipeline advances; 'failed' records the reason
# in error_message rather than raising into the submitting request.
PROJECT_STATUSES = ("pending", "fetching", "analyzing", "attesting", "completed", "failed")
PROJECT_VISIBILITIES = ("private", "public")

class Project(Base):
    """One submitted repository and the skills its owner claims it demonstrates."""

    __tablename__ = "projects"
    __table_args__ = (
        one_of("status", PROJECT_STATUSES),
        one_of("visibility", PROJECT_VISIBILITIES),
    )

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(
        String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    github_repo_url = Column(String, nullable=False)
    # Parsed from github_repo_url at submission so API calls need no re-parsing.
    repo_owner = Column(String, nullable=False)
    repo_name = Column(String, nullable=False)
    live_demo_url = Column(String, nullable=True)
    skills_claimed = Column(JSON, nullable=True)
    role_in_project = Column(String, nullable=True)
    status = Column(String, nullable=False, default="pending", server_default="pending")
    error_message = Column(Text, nullable=True)
    visibility = Column(String, nullable=False, default="private", server_default="private")
    submitted_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="projects")
    snapshots = relationship(
        "RepoSnapshot",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RepoSnapshot.fetched_at",
    )
