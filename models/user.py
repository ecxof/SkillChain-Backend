from sqlalchemy import Column, String, Boolean, DateTime
from sqlalchemy.sql import func
from db.database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True)
    email = Column(String, unique=True, nullable=False)
    google_id = Column(String, unique=True, nullable=True)
    github_id = Column(String, unique=True, nullable=True)
    github_access_token = Column(String, nullable=True)
    display_name = Column(String, nullable=True)
    avatar_url = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())