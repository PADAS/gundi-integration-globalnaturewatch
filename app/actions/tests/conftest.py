"""Fixtures for tests that need a real patch store.

They run against the PostgreSQL database named by GNW_PATCHES_TEST_DB_URL and
are skipped when it is unset, except in CI, where a missing database fails
the run so these tests cannot go quiet unnoticed.

Every test gets a schema freshly built by the migrations. The schema is
dropped, not rolled back, because the locking tests need several connections
that really commit.
Point the URL at a scratch database only.
"""
import os

import pytest
import pytest_asyncio

from app.actions.patches_store import db, migrate

TEST_DB_URL_VAR = "GNW_PATCHES_TEST_DB_URL"


@pytest_asyncio.fixture
async def empty_db_url():
    """Connection string for a database with no patch store in it."""
    url = os.environ.get(TEST_DB_URL_VAR)
    if not url:
        if os.environ.get("CI"):
            pytest.fail(f"{TEST_DB_URL_VAR} is not set: the patch store tests must run in CI.")
        pytest.skip(f"{TEST_DB_URL_VAR} is not set")
    async with db.connect(url) as conn:
        async with conn.transaction():
            await conn.execute("DROP SCHEMA IF EXISTS gnw_patches CASCADE")
    return url


@pytest_asyncio.fixture
async def store_url(empty_db_url):
    """Connection string for a database holding a new, empty patch store."""
    migrate.upgrade(empty_db_url)
    return empty_db_url


@pytest_asyncio.fixture
async def conn(store_url):
    """One connection to the test store. Changes it makes are visible to
    other connections only after a commit."""
    async with db.connect(store_url) as connection:
        yield connection
