"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""
from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    # Schema-qualify every name (gnw_patches.<table>): the search_path is
    # not set for you.
    op.execute(
        """
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Patch store migrations are forward-only.")
