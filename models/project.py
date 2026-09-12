from sqlalchemy import Column, String, Integer, DateTime, JSON
from sqlalchemy.sql import func
from db.database import Base

class Project(Base):
    __tablename__ = "projects"

    id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False)
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    github_repo_url = Column(String, nullable=False)
    live_demo_url = Column(String, nullable=True)
    skills_claimed = Column(JSON, nullable=True)
    role_in_project = Column(String, nullable=True)
    status = Column(String, default="pending")
    repo_languages = Column(JSON, nullable=True)
    commit_count = Column(Integer, nullable=True)
    readme_content = Column(String, nullable=True)
    submitted_at = Column(DateTime(timezone=True), server_default=func.now())