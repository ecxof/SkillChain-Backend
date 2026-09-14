from sqlalchemy import CheckConstraint, MetaData, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from dotenv import load_dotenv
import os

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# Explicit constraint names, so migrations refer to the same names on every
# database instead of whatever each backend would generate on its own.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base(metadata=MetaData(naming_convention=NAMING_CONVENTION))


def one_of(column, values):
    """CHECK constraint restricting a string column to a fixed set of values.

    Used instead of native ENUM types, which need awkward migrations to add a
    value and are not portable to SQLite.
    """
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=column)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
