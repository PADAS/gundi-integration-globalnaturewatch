"""The migration runner: it builds the store from nothing, is safe to run
again and in parallel, and never goes backwards."""
import asyncio
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from app.actions.patches_store import db, migrate, store

from .patch_store_fixtures import DATASET, make_patch

TABLES = {
    "aoi", "watched_tile", "aoi_tile", "patch", "patch_pixel", "patch_change_head",
    "patch_change", "aoi_patch", "delivery", "delivery_cursor", "alembic_version",
}


async def _tables(url):
    async with db.connect(url) as conn:
        cur = await conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'gnw_patches'")
        return {row[0] for row in await cur.fetchall()}


def test_revisions_form_one_line_numbered_in_order():
    scripts = ScriptDirectory.from_config(migrate._config())
    assert len(scripts.get_heads()) == 1
    revisions = [s.revision for s in scripts.walk_revisions()][::-1]
    assert revisions == [f"{n:04d}" for n in range(1, len(revisions) + 1)]
    assert migrate.head() == revisions[-1]


@pytest.mark.asyncio
async def test_upgrade_builds_the_store_from_an_empty_database(empty_db_url):
    assert migrate.current(empty_db_url) is None

    migrate.upgrade(empty_db_url)

    assert migrate.current(empty_db_url) == migrate.head()
    assert await _tables(empty_db_url) == TABLES
    # Spec P2-3 names these two columns.
    async with db.connect(empty_db_url) as conn:
        cur = await conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'gnw_patches' AND table_name = 'patch'")
        assert {"driver_source", "driver_coverage"} <= {row[0] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_upgrade_again_changes_nothing_and_keeps_the_data(store_url):
    async with db.connect(store_url) as conn:
        patch = await store.insert_patch(conn, make_patch())
        await store.append_change(conn, DATASET, patch.patch_id, 1, ["created"])
        await conn.commit()

    migrate.upgrade(store_url)

    assert migrate.current(store_url) == migrate.head()
    async with db.connect(store_url) as conn:
        assert await store.get_patch(conn, patch.patch_id) == patch
        assert await store.feed_head(conn, DATASET) == 1


@pytest.mark.asyncio
async def test_simultaneous_upgrades_both_succeed(empty_db_url):
    # Separate processes, as separate deploys would be: Alembic keeps its
    # running context in a module global, so threads cannot share it.
    loop = asyncio.get_running_loop()
    spawn = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=3, mp_context=spawn) as pool:
        await asyncio.gather(*(
            loop.run_in_executor(pool, migrate.upgrade, empty_db_url) for _ in range(3)))

    assert migrate.current(empty_db_url) == migrate.head()
    assert await _tables(empty_db_url) == TABLES


@pytest.mark.asyncio
async def test_migrations_do_not_go_backwards(store_url):
    config = migrate._config()
    with migrate._connection(store_url) as connection:
        config.attributes["connection"] = connection
        with pytest.raises(NotImplementedError):
            command.downgrade(config, "base")

    assert migrate.current(store_url) == migrate.head()
    assert await _tables(store_url) == TABLES


def test_new_revision_follows_the_head(tmp_path, mocker):
    # Write into a copy, so the test leaves no file in the real versions folder.
    import shutil
    copy = tmp_path / "migrations"
    shutil.copytree(migrate.MIGRATIONS_DIR, copy)
    mocker.patch.object(migrate, "MIGRATIONS_DIR", copy)
    following = f"{int(migrate.head()) + 1:04d}"

    path = migrate.new_revision("Add a thing")

    assert path == str(copy / "versions" / f"{following}_add_a_thing.py")
    assert migrate.head() == following
    assert "forward-only" in (copy / "versions" / f"{following}_add_a_thing.py").read_text()


def test_upgrade_needs_a_configured_store(mocker):
    mocker.patch("app.settings.GNW_PATCHES_DB_HOST", None)
    with pytest.raises(db.PatchStoreNotConfigured):
        migrate.upgrade()
