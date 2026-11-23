"""Connections to the patch store.

One connection per unit of work (one curated tile, one connection's delivery
run), opened here and closed when the block ends. There is no pool: the
database shares a small instance with other services, and a runner that holds
no idle connections cannot crowd them out.
"""
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

import psycopg
from psycopg.conninfo import make_conninfo

SQL_DIR = Path(__file__).parent / "sql"


class PatchStoreNotConfigured(RuntimeError):
    """The runner has no patch store settings."""


def conninfo_from_settings() -> str:
    """Connection string built from the GNW_PATCHES_DB_* settings."""
    from app import settings

    if not settings.GNW_PATCHES_DB_HOST:
        raise PatchStoreNotConfigured(
            "GNW_PATCHES_DB_HOST is not set, so the patch store cannot be reached."
        )
    return make_conninfo(
        host=settings.GNW_PATCHES_DB_HOST,
        port=settings.GNW_PATCHES_DB_PORT,
        dbname=settings.GNW_PATCHES_DB_NAME,
        user=settings.GNW_PATCHES_DB_USER,
        password=settings.GNW_PATCHES_DB_PASSWORD,
        connect_timeout=settings.GNW_PATCHES_DB_CONNECT_TIMEOUT,
        application_name="gnw-patches",
    )


@asynccontextmanager
async def connect(conninfo: Optional[str] = None) -> AsyncIterator[psycopg.AsyncConnection]:
    """Open a connection and close it when the block ends.

    The connection is not in autocommit mode. Wrap each unit of work in
    `async with conn.transaction():` so it commits or rolls back as a whole;
    work left outside one is rolled back when the connection closes.
    """
    conn = await psycopg.AsyncConnection.connect(conninfo or conninfo_from_settings())
    try:
        yield conn
    finally:
        await conn.close()


def schema_sql(name: str = "0001_initial.sql") -> str:
    """The text of one schema file. It must run inside a transaction."""
    return (SQL_DIR / name).read_text()
