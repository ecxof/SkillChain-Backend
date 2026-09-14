import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from db.database import Base, one_of

# Whether the fetch used the submitter's GitHub token (5000 requests/hour,
# private repositories) or no credentials at all (60/hour, public only).
FETCH_MODES = ("authenticated", "public")

class RepoSnapshot(Base):
    """Exactly what was fetched from GitHub for one analysis run.

    Stored in full, including the sampled file contents, so a report can be
    reproduced from the same inputs and its evidence line ranges still resolve
    after the repository has moved on.
    """

    __tablename__ = "repo_snapshots"
    __table_args__ = (one_of("fetch_mode", FETCH_MODES),)

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(
        String, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The exact commit the analysis saw; re-running against it must reproduce.
    commit_sha = Column(String(64), nullable=False)
    default_branch = Column(String, nullable=True)
    languages = Column(JSON, nullable=True)
    topics = Column(JSON, nullable=True)
    stars = Column(Integer, nullable=True)
    forks = Column(Integer, nullable=True)
    commit_count = Column(Integer, nullable=True)
    # Commits by the submitter alone, against commit_count for the whole repo.
    user_commit_count = Column(Integer, nullable=True)
    readme_content = Column(Text, nullable=True)
    file_tree = Column(JSON, nullable=True)
    # {path: content} for the bounded sample of files sent to the model.
    sampled_files = Column(JSON, nullable=True)
    # Commit metadata and per-author statistics behind the authorship signals.
    commit_history = Column(JSON, nullable=True)
    contributor_stats = Column(JSON, nullable=True)
    fetch_mode = Column(String, nullable=False)
    fetched_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    project = relationship("Project", back_populates="snapshots")
    report = relationship(
        "AnalysisReport",
        back_populates="snapshot",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
