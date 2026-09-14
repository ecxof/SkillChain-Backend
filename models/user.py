from sqlalchemy import Boolean, Column, DateTime, String, false
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from db.database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True)
    email = Column(String, unique=True, nullable=False)
    google_id = Column(String, unique=True, nullable=True)
    github_id = Column(String, unique=True, nullable=True)
    github_username = Column(String, nullable=True)
    github_access_token = Column(String, nullable=True)
    display_name = Column(String, nullable=True)
    avatar_url = Column(String, nullable=True)
    # Public profile at /public/profiles/{slug}; only served when opted in.
    profile_slug = Column(String, unique=True, nullable=True)
    profile_is_public = Column(Boolean, nullable=False, default=False, server_default=false())
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    projects = relationship(
        "Project", back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
