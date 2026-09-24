import os

# Set before any application module is imported. load_dotenv() never overrides
# variables that already exist, so tests use in-memory SQLite and cannot touch
# the development database configured in .env.
os.environ.setdefault("DATABASE_URL", "sqlite://")
# services.auth_service reads SECRET_KEY at import time, so a clean checkout
# with no .env would otherwise sign tokens with None and fail every
# authenticated test for a reason that looks nothing like the cause.
os.environ.setdefault("SECRET_KEY", "test-secret-key-at-least-32-bytes-long!")

import uuid  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import models  # noqa: E402,F401
from db.database import Base, get_db  # noqa: E402
from main import app  # noqa: E402


@pytest.fixture
def db_session():
    """A fresh in-memory database per test, built from the models.

    StaticPool keeps a single connection, so the test and the app, which
    TestClient runs in another thread, see the same in-memory database.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    # SQLite ignores foreign keys unless asked. Without this the ON DELETE
    # CASCADE rules that PostgreSQL enforces are silently absent, and the
    # models' passive_deletes=True means SQLAlchemy will not stand in for
    # them either, so deletes would leave orphans only in tests.
    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def user(db_session):
    """A signed-up user owning nothing yet."""
    from models.user import User

    record = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4()}@example.test",
                  display_name="Ada Lovelace", github_username="ada")
    db_session.add(record)
    db_session.commit()
    return record


@pytest.fixture
def auth_headers(user):
    """Bearer credentials for :func:`user`, as a real client would send them."""
    from services.auth_service import create_access_token

    return {"Authorization": f"Bearer {create_access_token({'sub': user.id})}"}


@pytest.fixture
def other_user(db_session):
    """A second user, for checking one account cannot reach another's work."""
    from models.user import User

    record = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4()}@example.test")
    db_session.add(record)
    db_session.commit()
    return record
