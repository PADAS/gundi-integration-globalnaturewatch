"""The guarantees the schema itself makes, checked with plain SQL: its
constraints, its cascades, and the gap-free, commit-ordered change feed."""
import asyncio
from uuid import uuid4

import psycopg
import pytest

from app.actions.patches_store import db, store

from .patch_store_fixtures import DATASET, add_aoi, make_patch

pytestmark = pytest.mark.asyncio

APPEND = "SELECT gnw_patches.append_patch_change(%s, %s, 1, ARRAY['created'])"


async def _seqs(conn, dataset=DATASET):
    cur = await conn.execute(
        "SELECT seq FROM gnw_patches.patch_change WHERE dataset = %s ORDER BY seq", (dataset,)
    )
    return [row[0] for row in await cur.fetchall()]


async def _stored_patch(conn):
    patch = await store.insert_patch(conn, make_patch())
    await conn.commit()
    return patch.patch_id


@pytest.mark.parametrize("overrides", [
    {"status": "deleted"},
    {"status": "merged"},                          # merged without a survivor
    {"min_row": 5, "max_row": 4},
    {"min_col": 5, "max_col": 4},
    {"pixel_count": -1},
])
async def test_patch_constraints_reject_bad_rows(conn, overrides):
    with pytest.raises(psycopg.errors.CheckViolation):
        await store.insert_patch(conn, make_patch(**overrides))


async def test_patch_rejects_alert_dates_out_of_order(conn):
    good = make_patch()
    with pytest.raises(psycopg.errors.CheckViolation):
        await store.insert_patch(
            conn, make_patch(first_alert=good.last_alert, last_alert=good.first_alert)
        )


async def test_only_a_merged_patch_may_point_at_a_survivor(conn):
    survivor = await store.insert_patch(conn, make_patch())
    await store.insert_patch(conn, make_patch(status="merged", merged_into=survivor.patch_id))
    with pytest.raises(psycopg.errors.CheckViolation):
        await store.insert_patch(conn, make_patch(status="open", merged_into=survivor.patch_id))


async def test_a_pixel_must_belong_to_a_stored_patch(conn):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await conn.execute(
            "INSERT INTO gnw_patches.patch_pixel VALUES (%s, 1, 1, '2026-11-01', 2, NULL, %s)",
            (DATASET, uuid4()),
        )


async def test_a_tile_cannot_be_watched_before_it_is_registered(conn):
    aoi = await add_aoi(conn, tiles=())
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await conn.execute(
            "INSERT INTO gnw_patches.aoi_tile VALUES (%s, %s, '00N_000E')", (aoi.aoi_id, DATASET)
        )


async def test_a_connection_has_one_aoi(conn):
    aoi = await add_aoi(conn)
    with pytest.raises(psycopg.errors.UniqueViolation):
        await conn.execute(
            "INSERT INTO gnw_patches.aoi VALUES (%s, %s, '{}', NULL, now())",
            (uuid4(), aoi.connection_id),
        )


async def test_feed_numbers_each_dataset_from_one(conn):
    patch_id = await _stored_patch(conn)
    for _ in range(3):
        await conn.execute(APPEND, (DATASET, patch_id))
    await conn.execute(APPEND, ("other_dataset", patch_id))
    await conn.commit()

    assert await _seqs(conn) == [1, 2, 3]
    assert await _seqs(conn, "other_dataset") == [1]


async def test_a_rolled_back_append_leaves_no_gap(conn):
    patch_id = await _stored_patch(conn)
    await conn.execute(APPEND, (DATASET, patch_id))
    await conn.commit()
    await conn.execute(APPEND, (DATASET, patch_id))
    await conn.rollback()
    await conn.execute(APPEND, (DATASET, patch_id))
    await conn.commit()

    assert await _seqs(conn) == [1, 2]


async def test_parallel_writers_leave_no_gaps(conn, store_url):
    patch_id = await _stored_patch(conn)
    writers, appends_each = 6, 5

    async def writer(number):
        async with db.connect(store_url) as c:
            for attempt in range(appends_each):
                await c.execute(APPEND, (DATASET, patch_id))
                # Every third transaction fails after taking its number.
                if (number + attempt) % 3 == 0:
                    await c.rollback()
                else:
                    await c.commit()

    await asyncio.gather(*(writer(n) for n in range(writers)))

    committed = sum(
        1 for n in range(writers) for a in range(appends_each) if (n + a) % 3 != 0
    )
    assert await _seqs(conn) == list(range(1, committed + 1))
    assert await store.feed_head(conn, DATASET) == committed


async def test_a_second_writer_waits_so_seq_order_is_commit_order(conn, store_url):
    patch_id = await _stored_patch(conn)

    async with db.connect(store_url) as first, db.connect(store_url) as second:
        cur = await first.execute(APPEND, (DATASET, patch_id))
        assert (await cur.fetchone())[0] == 1

        async def append_second():
            cur = await second.execute(APPEND, (DATASET, patch_id))
            seq = (await cur.fetchone())[0]
            await second.commit()
            return seq

        waiting = asyncio.ensure_future(append_second())
        await asyncio.sleep(0.3)
        # The second writer is blocked, and a reader sees neither change, so
        # no reader can move its cursor past a number that is still pending.
        assert not waiting.done()
        assert await _seqs(conn) == []

        await first.commit()
        assert await asyncio.wait_for(waiting, timeout=5) == 2

    assert await _seqs(conn) == [1, 2]


async def test_writers_to_different_datasets_do_not_wait(conn, store_url):
    patch_id = await _stored_patch(conn)

    async with db.connect(store_url) as first, db.connect(store_url) as second:
        await first.execute(APPEND, (DATASET, patch_id))
        await asyncio.wait_for(second.execute(APPEND, ("other_dataset", patch_id)), timeout=5)
        await second.commit()
        await first.commit()


async def test_deleting_an_aoi_removes_its_links_and_nothing_else(conn):
    aoi = await add_aoi(conn)
    patch = await store.insert_patch(conn, make_patch())
    await store.set_patch_aois(conn, patch.patch_id, {aoi.aoi_id: 100.0})

    assert await store.delete_aoi(conn, aoi.connection_id)

    for table in ("aoi_tile", "aoi_patch"):
        cur = await conn.execute(f"SELECT count(*) FROM gnw_patches.{table}")
        assert (await cur.fetchone())[0] == 0
    assert await store.get_patch(conn, patch.patch_id) is not None
    assert await store.get_tile(conn, DATASET, "10N_010E") is not None
