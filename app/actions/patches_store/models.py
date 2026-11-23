"""Rows of the patch store, one dataclass per table the store reads back.

Field names match the column names in `sql/0001_initial.sql`, so a query's
result maps onto a dataclass by name.
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from uuid import UUID


@dataclass(frozen=True)
class BBox:
    """Inclusive bounding box in grid cells."""
    min_row: int
    max_row: int
    min_col: int
    max_col: int

    def __post_init__(self):
        if self.min_row > self.max_row or self.min_col > self.max_col:
            raise ValueError(f"Empty bounding box: {self}")

    def expanded(self, cells: int) -> "BBox":
        """The box grown by `cells` on every side (the adjacency halo)."""
        return BBox(self.min_row - cells, self.max_row + cells,
                    self.min_col - cells, self.max_col + cells)


@dataclass
class Aoi:
    aoi_id: UUID
    connection_id: UUID
    geojson: Dict[str, Any]
    geostore_id: Optional[str]
    resolved_at: datetime


@dataclass
class WatchedTile:
    dataset: str
    tile_id: str
    watermark_date: Optional[date]
    drivers_version: Optional[str]
    curated_at: Optional[datetime]


@dataclass
class Patch:
    """A patch row. `created_order` and `created_at` are assigned by the
    database on insert; leave them None on a patch that is not stored yet."""
    patch_id: UUID
    dataset: str
    tile_id: str
    status: str
    first_alert: date
    last_alert: date
    seed_row: int
    seed_col: int
    min_row: int
    max_row: int
    min_col: int
    max_col: int
    pixel_count: int
    area_m2: float
    max_confidence: int
    merged_into: Optional[UUID] = None
    hull: Optional[Dict[str, Any]] = None
    driver: Optional[str] = None
    driver_share: Optional[float] = None
    driver_coverage: bool = False
    drivers_version: Optional[str] = None
    driver_source: Optional[str] = None
    version: int = 1
    created_order: Optional[int] = None
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class Pixel:
    cell_row: int
    cell_col: int
    alert_date: date
    confidence: int
    driver_class: Optional[int] = None


@dataclass(frozen=True)
class StoredPixel:
    """A pixel as read back, with the patch that owns it."""
    patch_id: UUID
    cell_row: int
    cell_col: int
    alert_date: date
    confidence: int
    driver_class: Optional[int] = None


@dataclass
class PatchChange:
    dataset: str
    seq: int
    patch_id: UUID
    patch_version: int
    kinds: List[str]
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None


@dataclass
class Delivery:
    connection_id: UUID
    patch_id: UUID
    gundi_event_id: UUID
    delivered_version: int
    er_status: str
    delivered_at: Optional[datetime] = None
