from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context

from db.database import DATABASE_URL, Base

# Importing the models package registers every table on Base.metadata;
# autogenerate cannot see tables it was never told about.
import models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set; add it to .env")

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit migration SQL to stdout without connecting (alembic ... --sql)."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the database in DATABASE_URL."""
    # The URL goes straight to create_engine rather than through alembic.ini,
    # where ConfigParser interpolation would mangle '%' in encoded passwords.
    connectable = create_engine(DATABASE_URL, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
