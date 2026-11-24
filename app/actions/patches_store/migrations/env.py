"""Alembic environment for the patch store.

Run through `app.actions.patches_store.migrate`, which supplies the database
connection; there is no alembic.ini.

- Migrations are hand-written SQL. There are no SQLAlchemy models, so
  autogenerate is not available.
- Alembic's version table lives in the `gnw_patches` schema, beside the
  tables it describes.
- Each migration runs in its own transaction, so one migration's
  `SET LOCAL` does not carry into the next.
- A session advisory lock makes a second, simultaneous run wait for the
  first and then find nothing left to do.
"""
from alembic import context
from sqlalchemy import text

SCHEMA = "gnw_patches"

if context.is_offline_mode():
    raise RuntimeError("Patch store migrations run against a live database only.")

connection = context.config.attributes["connection"]
connection.execute(text("SELECT pg_advisory_lock(hashtext('gnw_patches.migrate'))"))
connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
connection.commit()

context.configure(
    connection=connection,
    target_metadata=None,
    version_table_schema=SCHEMA,
    transaction_per_migration=True,
)
with context.begin_transaction():
    context.run_migrations()
