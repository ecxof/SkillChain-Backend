"""Importing this package registers every model on Base.metadata.

Relationships reference other models by class name, so all of them must be
imported before mappers are configured, and Alembic's autogenerate only sees
tables it has been told about. Importing any single model module runs this
file first, so both hold no matter which module is imported.
"""

from . import analysis_report, project, repo_snapshot, skill_assessment, user  # noqa: F401
