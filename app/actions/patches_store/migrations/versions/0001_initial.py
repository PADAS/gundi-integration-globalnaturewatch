"""Initial patch store schema.

Revision ID: 0001
Revises:
"""
from alembic import op

from app.actions.patches_store.db import schema_sql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The file is many statements, one of them a function body. Run it on the
    # driver's own cursor, without parameters, so it goes to the server as
    # written; it joins this migration's transaction.
    cursor = op.get_bind().connection.cursor()
    try:
        cursor.execute(schema_sql("0001_initial.sql"))
    finally:
        cursor.close()


def downgrade() -> None:
    raise NotImplementedError("Patch store migrations are forward-only.")
