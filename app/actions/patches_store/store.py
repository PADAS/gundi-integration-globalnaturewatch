"""Queries against the patch store.

Every function takes an open connection and runs inside whatever transaction
the caller has started; none of them commits. That lets a caller make one
patch change, its pixels and its feed entry succeed or fail together (FR-7).

Table names are schema-qualified, so the connection's search_path does not
matter. No SQL here is built from caller-supplied text.
"""
from dataclasses import replace
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from .models import Aoi, BBox, Delivery, Patch, PatchChange, Pixel, StoredPixel, WatchedTile

OPEN, CLOSED, MERGED, WITHDRAWN = "open", "closed", "merged", "withdrawn"

CHANGE_KINDS = frozenset({
    "created", "grown", "shrunk", "confidence_changed",
    "driver_assigned", "merged", "withdrawn", "closed",
})

ER_STATUSES = frozenset({"active", "resolved"})


class PatchNotUpdated(Exception):
    """An update matched no row: the patch is missing, or the stored patch
    already has a higher version than the one being written."""


async def _fetch_all(conn: AsyncConnection, row_type, query: str, params: Sequence[Any]) -> list:
    async with conn.cursor(row_factory=class_row(row_type)) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def _fetch_one(conn: AsyncConnection, row_type, query: str, params: Sequence[Any]):
    async with conn.cursor(row_factory=class_row(row_type)) as cur:
        await cur.execute(query, params)
        return await cur.fetchone()


# --- AOIs -------------------------------------------------------------------

_AOI_COLS = "aoi_id, connection_id, geojson, geostore_id, resolved_at"


async def upsert_aoi(
    conn: AsyncConnection,
    *,
    connection_id: UUID,
    geojson: Mapping[str, Any],
    geostore_id: Optional[str],
    resolved_at: datetime,
) -> Aoi:
    """Store a connection's AOI, replacing the geometry it had before.
    A connection keeps the same `aoi_id` for life."""
    return await _fetch_one(
        conn, Aoi,
        f"""
        INSERT INTO gnw_patches.aoi ({_AOI_COLS})
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (connection_id) DO UPDATE
        SET geojson = EXCLUDED.geojson,
            geostore_id = EXCLUDED.geostore_id,
            resolved_at = EXCLUDED.resolved_at
        RETURNING {_AOI_COLS}
        """,
        (uuid4(), connection_id, Jsonb(dict(geojson)), geostore_id, resolved_at),
    )


async def get_aoi(conn: AsyncConnection, connection_id: UUID) -> Optional[Aoi]:
    return await _fetch_one(
        conn, Aoi,
        f"SELECT {_AOI_COLS} FROM gnw_patches.aoi WHERE connection_id = %s",
        (connection_id,),
    )


async def delete_aoi(conn: AsyncConnection, connection_id: UUID) -> bool:
    """Remove a connection's AOI with its tile and patch links. The tiles
    themselves stay, so their watermarks survive if another AOI returns."""
    cur = await conn.execute(
        "DELETE FROM gnw_patches.aoi WHERE connection_id = %s", (connection_id,)
    )
    return cur.rowcount > 0


async def set_aoi_tiles(
    conn: AsyncConnection, aoi_id: UUID, dataset: str, tile_ids: Iterable[str]
) -> None:
    """Make `tile_ids` the complete set of tiles this AOI watches in `dataset`."""
    tiles = sorted(set(tile_ids))
    await conn.execute(
        """
        INSERT INTO gnw_patches.watched_tile (dataset, tile_id)
        SELECT %s, t FROM unnest(%s::text[]) AS t
        ON CONFLICT DO NOTHING
        """,
        (dataset, tiles),
    )
    await conn.execute(
        """
        DELETE FROM gnw_patches.aoi_tile
        WHERE aoi_id = %s AND dataset = %s AND tile_id <> ALL(%s::text[])
        """,
        (aoi_id, dataset, tiles),
    )
    await conn.execute(
        """
        INSERT INTO gnw_patches.aoi_tile (aoi_id, dataset, tile_id)
        SELECT %s, %s, t FROM unnest(%s::text[]) AS t
        ON CONFLICT DO NOTHING
        """,
        (aoi_id, dataset, tiles),
    )


async def aois_watching_tile(conn: AsyncConnection, dataset: str, tile_id: str) -> List[Aoi]:
    return await _fetch_all(
        conn, Aoi,
        """
        SELECT a.aoi_id, a.connection_id, a.geojson, a.geostore_id, a.resolved_at
        FROM gnw_patches.aoi a
        JOIN gnw_patches.aoi_tile t ON t.aoi_id = a.aoi_id
        WHERE t.dataset = %s AND t.tile_id = %s
        ORDER BY a.aoi_id
        """,
        (dataset, tile_id),
    )


# --- Tiles ------------------------------------------------------------------

_TILE_COLS = "dataset, tile_id, watermark_date, drivers_version, curated_at"


async def get_tile(conn: AsyncConnection, dataset: str, tile_id: str) -> Optional[WatchedTile]:
    return await _fetch_one(
        conn, WatchedTile,
        f"SELECT {_TILE_COLS} FROM gnw_patches.watched_tile WHERE dataset = %s AND tile_id = %s",
        (dataset, tile_id),
    )


async def claim_tile(
    conn: AsyncConnection, dataset: str, *, curated_before: datetime
) -> Optional[WatchedTile]:
    """Lock and return the tile most overdue for curation, or None.

    Only tiles some AOI still watches, and not curated since `curated_before`,
    qualify. The row lock lasts until the caller's transaction ends, and a
    tile locked by another worker is skipped, not waited for, so parallel
    workers each get a different tile. Call `mark_tile_curated` in the same
    transaction, or the tile is handed out again.
    """
    return await _fetch_one(
        conn, WatchedTile,
        """
        SELECT t.dataset, t.tile_id, t.watermark_date, t.drivers_version, t.curated_at
        FROM gnw_patches.watched_tile t
        WHERE t.dataset = %s
          AND (t.curated_at IS NULL OR t.curated_at < %s)
          AND EXISTS (
              SELECT 1 FROM gnw_patches.aoi_tile a
              WHERE a.dataset = t.dataset AND a.tile_id = t.tile_id
          )
        ORDER BY t.curated_at NULLS FIRST, t.tile_id
        LIMIT 1
        FOR UPDATE OF t SKIP LOCKED
        """,
        (dataset, curated_before),
    )


async def mark_tile_curated(
    conn: AsyncConnection,
    dataset: str,
    tile_id: str,
    *,
    watermark_date: Optional[date],
    curated_at: datetime,
) -> None:
    """Record a finished curation run. The watermark never moves backwards;
    None (a run that saw no pixels) leaves it where it was."""
    await conn.execute(
        """
        UPDATE gnw_patches.watched_tile
        SET curated_at = %s,
            watermark_date = GREATEST(watermark_date, %s::date)
        WHERE dataset = %s AND tile_id = %s
        """,
        (curated_at, watermark_date, dataset, tile_id),
    )


# --- Patches ----------------------------------------------------------------

_PATCH_COLS = (
    "patch_id, dataset, tile_id, status, merged_into, created_order, created_at, "
    "first_alert, last_alert, seed_row, seed_col, min_row, max_row, min_col, max_col, "
    "pixel_count, area_m2, max_confidence, hull, driver, driver_share, driver_coverage, "
    "drivers_version, driver_source, version"
)


def _json_or_none(value: Optional[Mapping[str, Any]]) -> Optional[Jsonb]:
    return None if value is None else Jsonb(dict(value))


async def insert_patch(conn: AsyncConnection, patch: Patch) -> Patch:
    """Store a new patch and return it with the `created_order` and
    `created_at` the database assigned. Insert the patches of one batch in
    the order they were created, so the earliest gets the lowest order."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO gnw_patches.patch (
                patch_id, dataset, tile_id, status, merged_into,
                first_alert, last_alert, seed_row, seed_col,
                min_row, max_row, min_col, max_col,
                pixel_count, area_m2, max_confidence, hull,
                driver, driver_share, driver_coverage, drivers_version, driver_source,
                version
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING created_order, created_at
            """,
            (
                patch.patch_id, patch.dataset, patch.tile_id, patch.status, patch.merged_into,
                patch.first_alert, patch.last_alert, patch.seed_row, patch.seed_col,
                patch.min_row, patch.max_row, patch.min_col, patch.max_col,
                patch.pixel_count, patch.area_m2, patch.max_confidence,
                _json_or_none(patch.hull),
                patch.driver, patch.driver_share, patch.driver_coverage,
                patch.drivers_version, patch.driver_source, patch.version,
            ),
        )
        created_order, created_at = await cur.fetchone()
    return replace(patch, created_order=created_order, created_at=created_at)


async def update_patch(conn: AsyncConnection, patch: Patch) -> None:
    """Write a patch's current state over the stored row.

    Identity, seed, tile and creation order are fixed at insert and are not
    written. Raises PatchNotUpdated if the patch is not stored, or is stored
    at a higher version: versions only ever increase.
    """
    cur = await conn.execute(
        """
        UPDATE gnw_patches.patch
        SET status = %s, merged_into = %s,
            first_alert = %s, last_alert = %s,
            min_row = %s, max_row = %s, min_col = %s, max_col = %s,
            pixel_count = %s, area_m2 = %s, max_confidence = %s, hull = %s,
            driver = %s, driver_share = %s, driver_coverage = %s,
            drivers_version = %s, driver_source = %s,
            version = %s
        WHERE patch_id = %s AND version <= %s
        """,
        (
            patch.status, patch.merged_into,
            patch.first_alert, patch.last_alert,
            patch.min_row, patch.max_row, patch.min_col, patch.max_col,
            patch.pixel_count, patch.area_m2, patch.max_confidence,
            _json_or_none(patch.hull),
            patch.driver, patch.driver_share, patch.driver_coverage,
            patch.drivers_version, patch.driver_source,
            patch.version,
            patch.patch_id, patch.version,
        ),
    )
    if cur.rowcount == 0:
        raise PatchNotUpdated(
            f"Patch {patch.patch_id} is not stored, or is stored above version {patch.version}"
        )


async def get_patches(conn: AsyncConnection, patch_ids: Iterable[UUID]) -> Dict[UUID, Patch]:
    ids = list(patch_ids)
    if not ids:
        return {}
    rows = await _fetch_all(
        conn, Patch,
        f"SELECT {_PATCH_COLS} FROM gnw_patches.patch WHERE patch_id = ANY(%s::uuid[])",
        (ids,),
    )
    return {p.patch_id: p for p in rows}


async def get_patch(conn: AsyncConnection, patch_id: UUID) -> Optional[Patch]:
    return (await get_patches(conn, [patch_id])).get(patch_id)


async def find_patches_in_bbox(
    conn: AsyncConnection,
    dataset: str,
    bbox: BBox,
    *,
    statuses: Sequence[str] = (OPEN,),
) -> List[Patch]:
    """Patches whose bounding box overlaps `bbox`, earliest-created first.
    Overlapping boxes do not mean adjacent pixels: load the pixels to decide."""
    return await _fetch_all(
        conn, Patch,
        f"""
        SELECT {_PATCH_COLS} FROM gnw_patches.patch
        WHERE dataset = %s
          AND status = ANY(%s::text[])
          AND min_row <= %s AND max_row >= %s
          AND min_col <= %s AND max_col >= %s
        ORDER BY created_order
        """,
        (dataset, list(statuses), bbox.max_row, bbox.min_row, bbox.max_col, bbox.min_col),
    )


async def find_quiet_open_patches(
    conn: AsyncConnection, dataset: str, *, last_alert_before: date
) -> List[Patch]:
    """Open patches with no alert on or after `last_alert_before`: the ones
    due to close, wherever they are."""
    return await _fetch_all(
        conn, Patch,
        f"""
        SELECT {_PATCH_COLS} FROM gnw_patches.patch
        WHERE dataset = %s AND status = 'open' AND last_alert < %s
        ORDER BY created_order
        """,
        (dataset, last_alert_before),
    )


# --- Pixels -----------------------------------------------------------------

async def put_pixels(
    conn: AsyncConnection, dataset: str, patch_id: UUID, pixels: Iterable[Pixel]
) -> None:
    """Store pixels as members of `patch_id`.

    A pixel already stored with the same cell and alert date is overwritten
    and moves to this patch; a null `driver_class` keeps the stored class.
    A pixel whose alert date moved is a different row: delete the old one.
    `pixels` must not hold the same cell and date twice.
    """
    px = list(pixels)
    if not px:
        return
    await conn.execute(
        """
        INSERT INTO gnw_patches.patch_pixel
            (dataset, cell_row, cell_col, alert_date, confidence, driver_class, patch_id)
        SELECT %s, t.r, t.c, t.d, t.conf, t.cls, %s
        FROM unnest(%s::int[], %s::int[], %s::date[], %s::smallint[], %s::smallint[])
             AS t(r, c, d, conf, cls)
        ON CONFLICT (dataset, cell_row, cell_col, alert_date) DO UPDATE
        SET confidence = EXCLUDED.confidence,
            patch_id = EXCLUDED.patch_id,
            driver_class = COALESCE(EXCLUDED.driver_class, patch_pixel.driver_class)
        """,
        (
            dataset, patch_id,
            [p.cell_row for p in px], [p.cell_col for p in px],
            [p.alert_date for p in px], [p.confidence for p in px],
            [p.driver_class for p in px],
        ),
    )


async def delete_pixels(conn: AsyncConnection, dataset: str, pixels: Iterable[Pixel]) -> int:
    """Remove pixels by cell and alert date. Returns how many were stored."""
    px = list(pixels)
    if not px:
        return 0
    cur = await conn.execute(
        """
        DELETE FROM gnw_patches.patch_pixel p
        USING unnest(%s::int[], %s::int[], %s::date[]) AS t(r, c, d)
        WHERE p.dataset = %s
          AND p.cell_row = t.r AND p.cell_col = t.c AND p.alert_date = t.d
        """,
        (
            [p.cell_row for p in px], [p.cell_col for p in px],
            [p.alert_date for p in px], dataset,
        ),
    )
    return cur.rowcount


async def move_pixels(conn: AsyncConnection, *, from_patch_id: UUID, to_patch_id: UUID) -> int:
    """Hand every pixel of one patch to another (a merge). Returns the count."""
    cur = await conn.execute(
        "UPDATE gnw_patches.patch_pixel SET patch_id = %s WHERE patch_id = %s",
        (to_patch_id, from_patch_id),
    )
    return cur.rowcount


_PIXEL_COLS = "patch_id, cell_row, cell_col, alert_date, confidence, driver_class"


async def pixels_of_patches(conn: AsyncConnection, patch_ids: Iterable[UUID]) -> List[StoredPixel]:
    ids = list(patch_ids)
    if not ids:
        return []
    return await _fetch_all(
        conn, StoredPixel,
        f"""
        SELECT {_PIXEL_COLS} FROM gnw_patches.patch_pixel
        WHERE patch_id = ANY(%s::uuid[])
        ORDER BY cell_row, cell_col, alert_date
        """,
        (ids,),
    )


async def pixels_in_bbox(conn: AsyncConnection, dataset: str, bbox: BBox) -> List[StoredPixel]:
    """Every stored pixel inside `bbox`, whatever the state of its patch."""
    return await _fetch_all(
        conn, StoredPixel,
        f"""
        SELECT {_PIXEL_COLS} FROM gnw_patches.patch_pixel
        WHERE dataset = %s
          AND cell_row BETWEEN %s AND %s
          AND cell_col BETWEEN %s AND %s
        ORDER BY cell_row, cell_col, alert_date
        """,
        (dataset, bbox.min_row, bbox.max_row, bbox.min_col, bbox.max_col),
    )


# --- AOI membership ---------------------------------------------------------

async def set_patch_aois(
    conn: AsyncConnection, patch_id: UUID, in_aoi_area_m2: Mapping[UUID, float]
) -> None:
    """Make the given AOIs, each with the patch area inside it, the complete
    set of AOIs this patch belongs to. This decides which connections are
    told about the patch."""
    aoi_ids = list(in_aoi_area_m2)
    await conn.execute(
        "DELETE FROM gnw_patches.aoi_patch WHERE patch_id = %s AND aoi_id <> ALL(%s::uuid[])",
        (patch_id, aoi_ids),
    )
    if not aoi_ids:
        return
    await conn.execute(
        """
        INSERT INTO gnw_patches.aoi_patch (aoi_id, patch_id, in_aoi_area_m2)
        SELECT t.aoi_id, %s, t.area
        FROM unnest(%s::uuid[], %s::double precision[]) AS t(aoi_id, area)
        ON CONFLICT (aoi_id, patch_id) DO UPDATE
        SET in_aoi_area_m2 = EXCLUDED.in_aoi_area_m2
        """,
        (patch_id, aoi_ids, [float(in_aoi_area_m2[a]) for a in aoi_ids]),
    )


async def in_aoi_areas(
    conn: AsyncConnection, connection_id: UUID, patch_ids: Iterable[UUID]
) -> Dict[UUID, float]:
    """Area inside the connection's AOI for each of the given patches. A
    patch outside the AOI is absent from the result."""
    ids = list(patch_ids)
    if not ids:
        return {}
    cur = await conn.execute(
        """
        SELECT ap.patch_id, ap.in_aoi_area_m2
        FROM gnw_patches.aoi_patch ap
        JOIN gnw_patches.aoi a ON a.aoi_id = ap.aoi_id
        WHERE a.connection_id = %s AND ap.patch_id = ANY(%s::uuid[])
        """,
        (connection_id, ids),
    )
    return dict(await cur.fetchall())


# --- Change feed ------------------------------------------------------------

async def append_change(
    conn: AsyncConnection,
    dataset: str,
    patch_id: UUID,
    patch_version: int,
    kinds: Sequence[str],
    payload: Optional[Mapping[str, Any]] = None,
) -> int:
    """Append one change to the dataset's feed and return its seq.

    This takes the dataset's feed lock until the transaction ends, so other
    writers to the same dataset wait: append last, then commit promptly.
    """
    unknown = set(kinds) - CHANGE_KINDS
    if unknown or not kinds:
        raise ValueError(f"Change kinds must be a non-empty subset of {sorted(CHANGE_KINDS)}, got {list(kinds)}")
    cur = await conn.execute(
        "SELECT gnw_patches.append_patch_change(%s::text, %s::uuid, %s::int, %s::text[], %s::jsonb)",
        (dataset, patch_id, patch_version, list(kinds), Jsonb(dict(payload or {}))),
    )
    (seq,) = await cur.fetchone()
    return seq


async def feed_head(conn: AsyncConnection, dataset: str) -> int:
    """The seq of the newest committed change in the dataset; 0 if none."""
    cur = await conn.execute(
        "SELECT last_seq FROM gnw_patches.patch_change_head WHERE dataset = %s", (dataset,)
    )
    row = await cur.fetchone()
    return row[0] if row else 0


_CHANGE_COLS = "c.dataset, c.seq, c.patch_id, c.patch_version, c.kinds, c.payload, c.created_at"


async def read_changes(
    conn: AsyncConnection, dataset: str, *, after_seq: int, up_to_seq: Optional[int] = None,
    limit: int = 500,
) -> List[PatchChange]:
    """Changes for every patch in the dataset, in seq order. For shared jobs
    and tooling; delivery must use `read_changes_for_connection`."""
    return await _fetch_all(
        conn, PatchChange,
        f"""
        SELECT {_CHANGE_COLS} FROM gnw_patches.patch_change c
        WHERE c.dataset = %s AND c.seq > %s AND (%s::bigint IS NULL OR c.seq <= %s)
        ORDER BY c.seq
        LIMIT %s
        """,
        (dataset, after_seq, up_to_seq, up_to_seq, limit),
    )


async def read_changes_for_connection(
    conn: AsyncConnection, connection_id: UUID, dataset: str, *, after_seq: int,
    up_to_seq: Optional[int] = None, limit: int = 500,
) -> List[PatchChange]:
    """Changes after `after_seq` for patches inside the connection's AOI, in
    seq order. A connection never sees a patch it has no `aoi_patch` row for.

    Changes for other AOIs are skipped, so the last seq returned can trail
    the feed. To move a cursor past them, read `feed_head` first, pass it as
    `up_to_seq`, and when fewer than `limit` rows come back advance to it.
    """
    return await _fetch_all(
        conn, PatchChange,
        f"""
        SELECT {_CHANGE_COLS} FROM gnw_patches.patch_change c
        WHERE c.dataset = %s AND c.seq > %s AND (%s::bigint IS NULL OR c.seq <= %s)
          AND EXISTS (
              SELECT 1 FROM gnw_patches.aoi_patch ap
              JOIN gnw_patches.aoi a ON a.aoi_id = ap.aoi_id
              WHERE ap.patch_id = c.patch_id AND a.connection_id = %s
          )
        ORDER BY c.seq
        LIMIT %s
        """,
        (dataset, after_seq, up_to_seq, up_to_seq, connection_id, limit),
    )


# --- Delivery ---------------------------------------------------------------

async def get_cursor(conn: AsyncConnection, connection_id: UUID, dataset: str) -> int:
    """The seq of the last change this connection has delivered; 0 if none."""
    cur = await conn.execute(
        "SELECT last_seq FROM gnw_patches.delivery_cursor WHERE connection_id = %s AND dataset = %s",
        (connection_id, dataset),
    )
    row = await cur.fetchone()
    return row[0] if row else 0


async def advance_cursor(conn: AsyncConnection, connection_id: UUID, dataset: str, seq: int) -> None:
    """Move the connection's cursor forward to `seq`. It never moves back."""
    await conn.execute(
        """
        INSERT INTO gnw_patches.delivery_cursor AS dc (connection_id, dataset, last_seq)
        VALUES (%s, %s, %s)
        ON CONFLICT (connection_id, dataset) DO UPDATE
        SET last_seq = GREATEST(dc.last_seq, EXCLUDED.last_seq)
        """,
        (connection_id, dataset, seq),
    )


_DELIVERY_COLS = "connection_id, patch_id, gundi_event_id, delivered_version, er_status, delivered_at"


async def get_deliveries(
    conn: AsyncConnection, connection_id: UUID, patch_ids: Iterable[UUID]
) -> Dict[UUID, Delivery]:
    """What this connection has already sent, by patch."""
    ids = list(patch_ids)
    if not ids:
        return {}
    rows = await _fetch_all(
        conn, Delivery,
        f"""
        SELECT {_DELIVERY_COLS} FROM gnw_patches.delivery
        WHERE connection_id = %s AND patch_id = ANY(%s::uuid[])
        """,
        (connection_id, ids),
    )
    return {d.patch_id: d for d in rows}


async def record_delivery(
    conn: AsyncConnection,
    *,
    connection_id: UUID,
    patch_id: UUID,
    gundi_event_id: UUID,
    delivered_version: int,
    er_status: str,
) -> bool:
    """Record that Gundi accepted an event for this patch at this version.

    The Gundi event ID is fixed by the first delivery and never replaced.
    Returns False, changing nothing, when a higher version is already
    recorded, so a retried older delivery cannot roll the record back.
    """
    if er_status not in ER_STATUSES:
        raise ValueError(f"er_status must be one of {sorted(ER_STATUSES)}, got {er_status!r}")
    cur = await conn.execute(
        """
        INSERT INTO gnw_patches.delivery AS d
            (connection_id, patch_id, gundi_event_id, delivered_version, er_status)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (connection_id, patch_id) DO UPDATE
        SET delivered_version = EXCLUDED.delivered_version,
            er_status = EXCLUDED.er_status,
            delivered_at = now()
        WHERE d.delivered_version <= EXCLUDED.delivered_version
        """,
        (connection_id, patch_id, gundi_event_id, delivered_version, er_status),
    )
    return cur.rowcount > 0
