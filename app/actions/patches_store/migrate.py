"""Migrations for the patch store.

    python -m app.actions.patches_store.migrate              # upgrade to the newest revision
    python -m app.actions.patches_store.migrate current      # print the database's revision
    python -m app.actions.patches_store.migrate new "add x"  # write the next revision file

Migrations are forward-only: there is no downgrade. To undo a change, write
a new revision.
"""
import argparse
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import psycopg
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from .db import conninfo_from_settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
SCHEMA = "gnw_patches"


def _config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("file_template", "%%(rev)s_%%(slug)s")
    return config


@contextmanager
def _connection(conninfo: Optional[str]) -> Iterator[Connection]:
    # The engine gets its connections from psycopg directly, so the same
    # connection string works here and in `db.connect`, and the password is
    # never written into a URL.
    conninfo = conninfo or conninfo_from_settings()
    engine = create_engine(
        "postgresql+psycopg://", creator=lambda: psycopg.connect(conninfo), poolclass=NullPool
    )
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def head() -> str:
    """The newest revision in the code."""
    return ScriptDirectory.from_config(_config()).get_current_head()


def upgrade(conninfo: Optional[str] = None) -> None:
    """Bring the database to the newest revision. Safe to run at any time,
    and from several processes at once: a database already there is left
    alone. Not safe from several threads of one process."""
    config = _config()
    with _connection(conninfo) as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def current(conninfo: Optional[str] = None) -> Optional[str]:
    """The revision the database is at; None if it was never migrated."""
    with _connection(conninfo) as connection:
        context = MigrationContext.configure(connection, opts={"version_table_schema": SCHEMA})
        return context.get_current_revision()


def new_revision(message: str) -> str:
    """Write an empty revision file after the current head; returns its path.
    Revisions are numbered 0001, 0002, ... in order."""
    config = _config()
    latest = ScriptDirectory.from_config(config).get_current_head()
    script = command.revision(config, message=message, rev_id=f"{int(latest or 0) + 1:04d}")
    return script.path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Patch store migrations")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("upgrade", help="upgrade to the newest revision (the default)")
    commands.add_parser("current", help="print the database's revision")
    commands.add_parser("new", help="write the next revision file").add_argument("message")
    args = parser.parse_args(argv)

    if args.command == "current":
        print(f"database: {current()}  code: {head()}")
    elif args.command == "new":
        print(new_revision(args.message))
    else:
        before = current()
        upgrade()
        print(f"Patch store migrated: {before} -> {current()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
