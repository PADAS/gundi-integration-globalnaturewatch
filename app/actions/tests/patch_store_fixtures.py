"""Builders shared by the patch store tests."""
from datetime import date, datetime, timezone
from uuid import uuid4

from app.actions.patches_store import store
from app.actions.patches_store.models import Patch

DATASET = "gfw_integrated_alerts"
SQUARE = {
    "type": "Polygon",
    "coordinates": [[[10.0, 1.0], [10.1, 1.0], [10.1, 1.1], [10.0, 1.1], [10.0, 1.0]]],
}
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def make_patch(**overrides) -> Patch:
    values = dict(
        patch_id=uuid4(), dataset=DATASET, tile_id="10N_010E", status="open",
        first_alert=date(2026, 11, 1), last_alert=date(2026, 11, 5),
        seed_row=100, seed_col=200,
        min_row=100, max_row=102, min_col=200, max_col=203,
        pixel_count=4, area_m2=480.5, max_confidence=2,
    )
    values.update(overrides)
    return Patch(**values)


async def add_aoi(conn, connection_id=None, tiles=("10N_010E",)):
    aoi = await store.upsert_aoi(
        conn, connection_id=connection_id or uuid4(), geojson=SQUARE,
        geostore_id="geo-1", resolved_at=NOW,
    )
    await store.set_aoi_tiles(conn, aoi.aoi_id, DATASET, tiles)
    return aoi
