import os

# Set before any application module is imported. load_dotenv() never overrides
# variables that already exist, so tests use in-memory SQLite and cannot touch
# the development database configured in .env.
os.environ.setdefault("DATABASE_URL", "sqlite://")
