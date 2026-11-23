"""The store's query functions, against a real PostgreSQL database."""
from dataclasses import replace
from datetime import date, timedelta
from uuid import uuid4

import pytest

from app.actions.patches_store import db, store
from app.actions.patches_store.models import BBox, Pixel

from .patch_store_fixtures import DATASET, NOW, SQUARE, add_aoi, make_patch

pytestmark = pytest.mark.asyncio


# --- Connections ------------------------------------------------------------

async def test_conninfo_needs_a_host(mocker):
    mocker.patch("app.settings.GNW_PATCHES_DB_HOST", None)
    with pytest.raises(db.PatchStoreNotConfigured):
        db.conninfo_from_settings()


async def test_conninfo_is_built_from_settings(mocker):
    mocker.patch("app.settings.GNW_PATCHES_DB_HOST", "10.1.2.3")
    mocker.patch("app.settings.GNW_PATCHES_DB_PASSWORD", "p w'd")
    conninfo = db.conninfo_from_settings()
    assert "host=10.1.2.3" in conninfo
    assert "dbname=gnw_patches" in conninfo
    assert "user=gnw_patches" in conninfo
    assert r"password='p w\'d'" in conninfo


async def test_work_outside_a_committed_transaction_is_discarded(store_url):
    async with db.connect(store_url) as c:
        await add_aoi(c)
    async with db.connect(store_url) as c:
        cur = await c.execute("SELECT count(*) FROM gnw_patches.aoi")
        assert (await cur.fetchone())[0] == 0


# --- AOIs -------------------------------------------------------------------

async def test_upsert_aoi_keeps_the_id_and_replaces_the_geometry(conn):
    connection_id = uuid4()
    first = await store.upsert_aoi(
        conn, connection_id=connection_id, geojson=SQUARE, geostore_id="a", resolved_at=NOW)
    moved = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    second = await store.upsert_aoi(
        conn, connection_id=connection_id, geojson=moved, geostore_id="b",
        resolved_at=NOW + timedelta(days=1))

    assert second.aoi_id == first.aoi_id
    assert first.geojson == SQUARE
    stored = await store.get_aoi(conn, connection_id)
    assert stored == second
    assert stored.geojson == moved
    assert stored.geostore_id == "b"
    assert stored.resolved_at == NOW + timedelta(days=1)


async def test_get_aoi_for_an_unknown_connection(conn):
    assert await store.get_aoi(conn, uuid4()) is None
    assert await store.delete_aoi(conn, uuid4()) is False


async def test_set_aoi_tiles_replaces_the_set_and_keeps_tile_state(conn):
    aoi = await add_aoi(conn, tiles=("10N_010E", "10N_020E"))
    await store.mark_tile_curated(
        conn, DATASET, "10N_010E", watermark_date=date(2026, 11, 20), curated_at=NOW)

    await store.set_aoi_tiles(conn, aoi.aoi_id, DATASET, ["10N_020E", "00N_020E", "00N_020E"])

    cur = await conn.execute(
        "SELECT tile_id FROM gnw_patches.aoi_tile WHERE aoi_id = %s ORDER BY tile_id",
        (aoi.aoi_id,))
    assert [r[0] for r in await cur.fetchall()] == ["00N_020E", "10N_020E"]
    # The tile no AOI watches any more keeps its watermark.
    assert (await store.get_tile(conn, DATASET, "10N_010E")).watermark_date == date(2026, 11, 20)


async def test_set_aoi_tiles_leaves_other_datasets_alone(conn):
    aoi = await add_aoi(conn, tiles=("10N_010E",))
    await store.set_aoi_tiles(conn, aoi.aoi_id, "other_dataset", ["10N_010E"])
    await store.set_aoi_tiles(conn, aoi.aoi_id, DATASET, [])

    assert await store.aois_watching_tile(conn, DATASET, "10N_010E") == []
    assert await store.aois_watching_tile(conn, "other_dataset", "10N_010E") == [aoi]


async def test_aois_watching_tile(conn):
    one = await add_aoi(conn, tiles=("10N_010E", "10N_020E"))
    two = await add_aoi(conn, tiles=("10N_020E",))

    assert await store.aois_watching_tile(conn, DATASET, "10N_010E") == [one]
    watching = await store.aois_watching_tile(conn, DATASET, "10N_020E")
    assert sorted(a.aoi_id for a in watching) == sorted([one.aoi_id, two.aoi_id])


# --- Tiles ------------------------------------------------------------------

async def test_claim_tile_takes_the_most_overdue_watched_tile(conn):
    aoi = await add_aoi(conn, tiles=("A", "B", "C", "D"))
    await store.mark_tile_curated(conn, DATASET, "A", watermark_date=None, curated_at=NOW)
    await store.mark_tile_curated(
        conn, DATASET, "B", watermark_date=None, curated_at=NOW - timedelta(days=3))
    await store.mark_tile_curated(
        conn, DATASET, "C", watermark_date=None, curated_at=NOW - timedelta(days=2))
    # D was never curated, so it goes first; A is too recent to qualify.
    cutoff = NOW - timedelta(hours=12)

    claimed = []
    while True:
        tile = await store.claim_tile(conn, DATASET, curated_before=cutoff)
        if tile is None:
            break
        claimed.append(tile.tile_id)
        await store.mark_tile_curated(
            conn, DATASET, tile.tile_id, watermark_date=None, curated_at=NOW)

    assert claimed == ["D", "B", "C"]
    await store.set_aoi_tiles(conn, aoi.aoi_id, DATASET, [])


async def test_claim_tile_ignores_tiles_no_aoi_watches(conn):
    aoi = await add_aoi(conn, tiles=("A",))
    await store.set_aoi_tiles(conn, aoi.aoi_id, DATASET, [])
    assert await store.claim_tile(conn, DATASET, curated_before=NOW) is None


async def test_parallel_workers_claim_different_tiles(conn, store_url):
    await add_aoi(conn, tiles=("A", "B"))
    await conn.commit()

    async with db.connect(store_url) as one, db.connect(store_url) as two, \
            db.connect(store_url) as three:
        first = await store.claim_tile(one, DATASET, curated_before=NOW)
        second = await store.claim_tile(two, DATASET, curated_before=NOW)
        third = await store.claim_tile(three, DATASET, curated_before=NOW)
        assert {first.tile_id, second.tile_id} == {"A", "B"}
        assert third is None

        # A worker that fails releases its tile for the next one.
        await one.rollback()
        retry = await store.claim_tile(three, DATASET, curated_before=NOW)
        assert retry.tile_id == first.tile_id


async def test_watermark_never_moves_backwards(conn):
    await add_aoi(conn, tiles=("A",))
    mark = store.mark_tile_curated

    await mark(conn, DATASET, "A", watermark_date=date(2026, 11, 10), curated_at=NOW)
    await mark(conn, DATASET, "A", watermark_date=date(2026, 11, 5), curated_at=NOW)
    assert (await store.get_tile(conn, DATASET, "A")).watermark_date == date(2026, 11, 10)

    later = NOW + timedelta(hours=1)
    await mark(conn, DATASET, "A", watermark_date=None, curated_at=later)
    tile = await store.get_tile(conn, DATASET, "A")
    assert tile.watermark_date == date(2026, 11, 10)
    assert tile.curated_at == later

    await mark(conn, DATASET, "A", watermark_date=date(2026, 11, 12), curated_at=later)
    assert (await store.get_tile(conn, DATASET, "A")).watermark_date == date(2026, 11, 12)


# --- Patches ----------------------------------------------------------------

async def test_insert_patch_round_trips_and_orders_by_creation(conn):
    hull = {"type": "Polygon", "coordinates": [[[10.0, 1.0], [10.1, 1.0], [10.1, 1.1], [10.0, 1.0]]]}
    first = await store.insert_patch(conn, make_patch(hull=hull, driver="mining",
                                                      driver_share=0.75, driver_coverage=True,
                                                      drivers_version="v3", driver_source="gnw"))
    second = await store.insert_patch(conn, make_patch())

    assert first.created_order < second.created_order
    assert first.created_at is not None
    assert await store.get_patch(conn, first.patch_id) == first
    assert await store.get_patches(conn, [first.patch_id, second.patch_id, uuid4()]) == {
        first.patch_id: first, second.patch_id: second}
    assert await store.get_patches(conn, []) == {}
    assert await store.get_patch(conn, uuid4()) is None


async def test_update_patch_writes_state_but_not_identity(conn):
    survivor = await store.insert_patch(conn, make_patch())
    stored = await store.insert_patch(conn, make_patch())
    changed = replace(
        stored, status="merged", merged_into=survivor.patch_id, version=2,
        last_alert=date(2026, 11, 9), max_row=110, pixel_count=0, max_confidence=3,
        driver="wildfire", driver_share=0.5, hull={"type": "Polygon", "coordinates": []},
        # None of these may change after insert:
        tile_id="00N_000E", seed_row=1, seed_col=1, dataset="other", created_order=999,
    )

    await store.update_patch(conn, changed)

    expected = replace(
        changed, tile_id=stored.tile_id, seed_row=stored.seed_row, seed_col=stored.seed_col,
        dataset=stored.dataset, created_order=stored.created_order)
    assert await store.get_patch(conn, stored.patch_id) == expected


async def test_update_patch_refuses_an_older_version_or_a_missing_patch(conn):
    stored = await store.insert_patch(conn, make_patch(version=5))

    with pytest.raises(store.PatchNotUpdated):
        await store.update_patch(conn, replace(stored, version=4, pixel_count=99))
    with pytest.raises(store.PatchNotUpdated):
        await store.update_patch(conn, make_patch())

    assert await store.get_patch(conn, stored.patch_id) == stored


async def test_find_patches_in_bbox(conn):
    box = dict(min_row=100, max_row=102, min_col=200, max_col=203)
    inside = await store.insert_patch(conn, make_patch(**box))
    corner = await store.insert_patch(
        conn, make_patch(min_row=102, max_row=105, min_col=203, max_col=209))
    await store.insert_patch(conn, make_patch(min_row=103, max_row=105, min_col=200, max_col=203))
    await store.insert_patch(conn, make_patch(min_row=100, max_row=102, min_col=204, max_col=209))
    closed = await store.insert_patch(conn, make_patch(status="closed", **box))
    await store.insert_patch(conn, make_patch(dataset="other_dataset", **box))

    found = await store.find_patches_in_bbox(conn, DATASET, BBox(**box))
    assert [p.patch_id for p in found] == [inside.patch_id, corner.patch_id]

    with_closed = await store.find_patches_in_bbox(
        conn, DATASET, BBox(**box), statuses=("open", "closed"))
    assert [p.patch_id for p in with_closed] == [
        inside.patch_id, corner.patch_id, closed.patch_id]

    # A halo of one cell reaches the patches that only touched the box.
    assert len(await store.find_patches_in_bbox(conn, DATASET, BBox(**box).expanded(1))) == 4


async def test_bbox_must_not_be_empty():
    with pytest.raises(ValueError):
        BBox(min_row=2, max_row=1, min_col=0, max_col=0)


async def test_find_quiet_open_patches(conn):
    quiet = await store.insert_patch(conn, make_patch(last_alert=date(2026, 11, 5)))
    await store.insert_patch(conn, make_patch(last_alert=date(2026, 11, 6)))
    await store.insert_patch(conn, make_patch(last_alert=date(2026, 11, 5), status="closed"))

    found = await store.find_quiet_open_patches(
        conn, DATASET, last_alert_before=date(2026, 11, 6))
    assert [p.patch_id for p in found] == [quiet.patch_id]


# --- Pixels -----------------------------------------------------------------

D1, D2 = date(2026, 11, 1), date(2026, 11, 2)


def _cells(stored):
    return [(p.cell_row, p.cell_col, p.alert_date, p.confidence, p.driver_class) for p in stored]


async def test_put_and_read_pixels(conn):
    one = await store.insert_patch(conn, make_patch())
    two = await store.insert_patch(conn, make_patch())
    await store.put_pixels(conn, DATASET, one.patch_id, [
        Pixel(100, 201, D2, 2), Pixel(100, 200, D1, 1, driver_class=4)])
    await store.put_pixels(conn, DATASET, two.patch_id, [Pixel(300, 300, D1, 3)])
    await store.put_pixels(conn, DATASET, two.patch_id, [])

    mine = await store.pixels_of_patches(conn, [one.patch_id])
    assert _cells(mine) == [(100, 200, D1, 1, 4), (100, 201, D2, 2, None)]
    assert {p.patch_id for p in mine} == {one.patch_id}
    assert len(await store.pixels_of_patches(conn, [one.patch_id, two.patch_id])) == 3
    assert await store.pixels_of_patches(conn, []) == []


async def test_put_pixels_overwrites_the_same_cell_and_date(conn):
    one = await store.insert_patch(conn, make_patch())
    two = await store.insert_patch(conn, make_patch())
    await store.put_pixels(conn, DATASET, one.patch_id, [Pixel(100, 200, D1, 1, driver_class=4)])

    # Higher confidence, new owner, no class given: the stored class stays.
    await store.put_pixels(conn, DATASET, two.patch_id, [Pixel(100, 200, D1, 3)])

    assert await store.pixels_of_patches(conn, [one.patch_id]) == []
    assert _cells(await store.pixels_of_patches(conn, [two.patch_id])) == [(100, 200, D1, 3, 4)]


async def test_the_same_cell_can_hold_two_alert_dates(conn):
    patch = await store.insert_patch(conn, make_patch())
    much_later = D1 + timedelta(days=200)
    await store.put_pixels(conn, DATASET, patch.patch_id, [
        Pixel(100, 200, D1, 1), Pixel(100, 200, much_later, 2)])

    assert _cells(await store.pixels_of_patches(conn, [patch.patch_id])) == [
        (100, 200, D1, 1, None), (100, 200, much_later, 2, None)]


async def test_pixels_in_bbox(conn):
    patch = await store.insert_patch(conn, make_patch())
    other = await store.insert_patch(conn, make_patch(dataset="other_dataset"))
    await store.put_pixels(conn, DATASET, patch.patch_id, [
        Pixel(100, 200, D1, 1), Pixel(102, 203, D1, 1), Pixel(103, 200, D1, 1),
        Pixel(100, 204, D1, 1), Pixel(99, 200, D1, 1)])
    await store.put_pixels(conn, "other_dataset", other.patch_id, [Pixel(101, 201, D1, 1)])

    found = await store.pixels_in_bbox(conn, DATASET, BBox(100, 102, 200, 203))
    assert [(p.cell_row, p.cell_col) for p in found] == [(100, 200), (102, 203)]


async def test_delete_pixels(conn):
    patch = await store.insert_patch(conn, make_patch())
    await store.put_pixels(conn, DATASET, patch.patch_id, [
        Pixel(100, 200, D1, 1), Pixel(100, 201, D1, 1), Pixel(100, 202, D2, 1)])

    removed = await store.delete_pixels(conn, DATASET, [
        Pixel(100, 200, D1, 1),
        Pixel(100, 201, D2, 1),      # stored under another date: not removed
        Pixel(500, 500, D1, 1),      # never stored
    ])

    assert removed == 1
    assert await store.delete_pixels(conn, DATASET, []) == 0
    left = await store.pixels_of_patches(conn, [patch.patch_id])
    assert [(p.cell_col, p.alert_date) for p in left] == [(201, D1), (202, D2)]


async def test_move_pixels_hands_a_merged_patch_to_its_survivor(conn):
    survivor = await store.insert_patch(conn, make_patch())
    absorbed = await store.insert_patch(conn, make_patch())
    await store.put_pixels(conn, DATASET, survivor.patch_id, [Pixel(100, 200, D1, 1)])
    await store.put_pixels(conn, DATASET, absorbed.patch_id, [
        Pixel(100, 201, D1, 1), Pixel(100, 202, D1, 1)])

    moved = await store.move_pixels(
        conn, from_patch_id=absorbed.patch_id, to_patch_id=survivor.patch_id)

    assert moved == 2
    assert await store.pixels_of_patches(conn, [absorbed.patch_id]) == []
    assert len(await store.pixels_of_patches(conn, [survivor.patch_id])) == 3


# --- AOI membership ---------------------------------------------------------

async def test_set_patch_aois_replaces_the_set(conn):
    one, two, three = [await add_aoi(conn) for _ in range(3)]
    patch = await store.insert_patch(conn, make_patch())
    ids = [patch.patch_id]

    await store.set_patch_aois(conn, patch.patch_id, {one.aoi_id: 100.0, two.aoi_id: 40.0})
    await store.set_patch_aois(conn, patch.patch_id, {two.aoi_id: 55.5, three.aoi_id: 7})

    assert await store.in_aoi_areas(conn, one.connection_id, ids) == {}
    assert await store.in_aoi_areas(conn, two.connection_id, ids) == {patch.patch_id: 55.5}
    assert await store.in_aoi_areas(conn, three.connection_id, ids) == {patch.patch_id: 7.0}
    assert await store.in_aoi_areas(conn, two.connection_id, []) == {}

    await store.set_patch_aois(conn, patch.patch_id, {})
    assert await store.in_aoi_areas(conn, two.connection_id, ids) == {}


# --- Change feed ------------------------------------------------------------

async def test_append_and_read_changes(conn):
    patch = await store.insert_patch(conn, make_patch())
    other = await store.insert_patch(conn, make_patch(dataset="other_dataset"))

    assert await store.feed_head(conn, DATASET) == 0
    assert await store.append_change(conn, DATASET, patch.patch_id, 1, ["created"]) == 1
    assert await store.append_change(
        conn, DATASET, patch.patch_id, 2, ("grown", "confidence_changed"),
        {"pixel_count": 9}) == 2
    assert await store.append_change(conn, "other_dataset", other.patch_id, 1, ["created"]) == 1
    assert await store.append_change(conn, DATASET, patch.patch_id, 3, ["closed"]) == 3
    assert await store.feed_head(conn, DATASET) == 3

    changes = await store.read_changes(conn, DATASET, after_seq=0)
    assert [c.seq for c in changes] == [1, 2, 3]
    second = changes[1]
    assert (second.dataset, second.patch_id, second.patch_version) == (DATASET, patch.patch_id, 2)
    assert second.kinds == ["grown", "confidence_changed"]
    assert second.payload == {"pixel_count": 9}
    assert changes[0].payload == {}
    assert second.created_at is not None

    read = store.read_changes
    assert [c.seq for c in await read(conn, DATASET, after_seq=1)] == [2, 3]
    assert [c.seq for c in await read(conn, DATASET, after_seq=0, up_to_seq=2)] == [1, 2]
    assert [c.seq for c in await read(conn, DATASET, after_seq=0, limit=1)] == [1]
    assert await read(conn, DATASET, after_seq=3) == []


@pytest.mark.parametrize("kinds", [[], ["created", "exploded"], ["Created"]])
async def test_append_change_rejects_unknown_kinds(kinds):
    with pytest.raises(ValueError):
        await store.append_change(None, DATASET, uuid4(), 1, kinds)


async def test_a_change_and_its_patch_commit_or_fail_together(conn, store_url):
    async with conn.transaction():
        kept = await store.insert_patch(conn, make_patch())
        await store.append_change(conn, DATASET, kept.patch_id, 1, ["created"])

    with pytest.raises(RuntimeError):
        async with conn.transaction():
            lost = await store.insert_patch(conn, make_patch())
            await store.append_change(conn, DATASET, lost.patch_id, 1, ["created"])
            raise RuntimeError("curation failed after the append")

    async with db.connect(store_url) as reader:
        assert await store.get_patch(reader, lost.patch_id) is None
        assert [c.patch_id for c in await store.read_changes(reader, DATASET, after_seq=0)] == [
            kept.patch_id]
        assert await store.feed_head(reader, DATASET) == 1


async def test_a_connection_reads_only_changes_for_patches_in_its_aoi(conn):
    mine, theirs = await add_aoi(conn), await add_aoi(conn)
    in_mine = await store.insert_patch(conn, make_patch())
    in_theirs = await store.insert_patch(conn, make_patch())
    in_both = await store.insert_patch(conn, make_patch())
    in_none = await store.insert_patch(conn, make_patch())
    await store.set_patch_aois(conn, in_mine.patch_id, {mine.aoi_id: 1.0})
    await store.set_patch_aois(conn, in_theirs.patch_id, {theirs.aoi_id: 1.0})
    await store.set_patch_aois(conn, in_both.patch_id, {mine.aoi_id: 1.0, theirs.aoi_id: 1.0})
    for patch in (in_mine, in_theirs, in_both, in_none, in_mine):
        await store.append_change(conn, DATASET, patch.patch_id, 1, ["created"])

    read = store.read_changes_for_connection
    changes = await read(conn, mine.connection_id, DATASET, after_seq=0)
    assert [(c.seq, c.patch_id) for c in changes] == [
        (1, in_mine.patch_id), (3, in_both.patch_id), (5, in_mine.patch_id)]
    assert [c.seq for c in await read(conn, theirs.connection_id, DATASET, after_seq=0)] == [2, 3]
    assert await read(conn, uuid4(), DATASET, after_seq=0) == []

    assert [c.seq for c in await read(conn, mine.connection_id, DATASET, after_seq=1)] == [3, 5]
    assert [c.seq for c in await read(
        conn, mine.connection_id, DATASET, after_seq=0, up_to_seq=4)] == [1, 3]
    assert [c.seq for c in await read(
        conn, mine.connection_id, DATASET, after_seq=0, limit=2)] == [1, 3]


# --- Delivery ---------------------------------------------------------------

async def test_cursor_starts_at_zero_and_only_moves_forward(conn):
    connection_id = uuid4()
    assert await store.get_cursor(conn, connection_id, DATASET) == 0

    await store.advance_cursor(conn, connection_id, DATASET, 7)
    await store.advance_cursor(conn, connection_id, DATASET, 4)
    assert await store.get_cursor(conn, connection_id, DATASET) == 7

    await store.advance_cursor(conn, connection_id, DATASET, 9)
    assert await store.get_cursor(conn, connection_id, DATASET) == 9
    # Cursors are per connection and per dataset.
    assert await store.get_cursor(conn, connection_id, "other_dataset") == 0
    assert await store.get_cursor(conn, uuid4(), DATASET) == 0


async def test_record_delivery_keeps_the_event_id_and_the_newest_version(conn):
    connection_id, event_id = uuid4(), uuid4()
    patch = await store.insert_patch(conn, make_patch())
    record = dict(connection_id=connection_id, patch_id=patch.patch_id)

    assert await store.record_delivery(
        conn, gundi_event_id=event_id, delivered_version=2, er_status="active", **record)
    assert await store.record_delivery(
        conn, gundi_event_id=uuid4(), delivered_version=4, er_status="resolved", **record)
    # A retry of an older delivery changes nothing.
    assert not await store.record_delivery(
        conn, gundi_event_id=event_id, delivered_version=3, er_status="active", **record)

    delivery = (await store.get_deliveries(conn, connection_id, [patch.patch_id]))[patch.patch_id]
    assert delivery.gundi_event_id == event_id
    assert delivery.delivered_version == 4
    assert delivery.er_status == "resolved"
    assert delivery.delivered_at is not None


async def test_deliveries_are_per_connection(conn):
    mine, theirs = uuid4(), uuid4()
    patch = await store.insert_patch(conn, make_patch())
    await store.record_delivery(
        conn, connection_id=theirs, patch_id=patch.patch_id, gundi_event_id=uuid4(),
        delivered_version=1, er_status="active")

    assert await store.get_deliveries(conn, mine, [patch.patch_id]) == {}
    assert await store.get_deliveries(conn, mine, []) == {}
    assert list(await store.get_deliveries(conn, theirs, [patch.patch_id, uuid4()])) == [
        patch.patch_id]


async def test_record_delivery_rejects_an_unknown_status():
    with pytest.raises(ValueError):
        await store.record_delivery(
            None, connection_id=uuid4(), patch_id=uuid4(), gundi_event_id=uuid4(),
            delivered_version=1, er_status="closed")
