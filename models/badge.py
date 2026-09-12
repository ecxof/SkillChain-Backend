from sqlalchemy import Column, String, Integer, Boolean, DateTime
from sqlalchemy.sql import func
from db.database import Base

class Badge(Base):
    __tablename__ = "badges"

    id = Column(String, primary_key=True)
    project_id = Column(String, nullable=False)
    wallet_address = Column(String, nullable=False)
    skill_name = Column(String, nullable=False)
    skill_level = Column(String, nullable=True)
    trust_score = Column(Integer, nullable=True)
    verifier_address = Column(String, nullable=True)
    tx_hash = Column(String, nullable=True)
    is_soulbound = Column(Boolean, default=True)
    issued_at = Column(DateTime(timezone=True), server_default=func.now())