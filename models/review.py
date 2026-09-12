from sqlalchemy import Column, String, DateTime
from sqlalchemy.sql import func
from db.database import Base

class Review(Base):
    __tablename__ = "reviews"

    id = Column(String, primary_key=True)
    project_id = Column(String, nullable=False)
    reviewer_id = Column(String, nullable=False)
    decision = Column(String, nullable=False)
    comments = Column(String, nullable=True)
    reviewer_role = Column(String, nullable=True)
    reviewed_at = Column(DateTime(timezone=True), server_default=func.now())