"""Map a patch to the Gundi event that represents it in EarthRanger.

Pure logic: builds the payload for the create (`POST /v2/events/`) and for
every later update (`PATCH /v2/events/{id}/`). Sending them, and remembering
the Gundi event ID per connection, is the delivery action's job.

Spec: "EarthRanger mapping". Still unconfirmed and therefore conservative:
- Polygon geometry (P0-3). `include_geometry` is off by default; until it is
  confirmed that ER renders it, an event is a point at the seed pixel.
- Driver class names (P0-5). `DEFAULT_DRIVER_PRIORITY` uses the names in the
  spec's priority table; an unknown class falls back to the default priority.
"""
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Dict, List, Mapping, Optional, Tuple

from .core import MERGED, WITHDRAWN, Patch
from .drivers import DriverSummary
from .grid import CELLS_PER_DEGREE, cell_center, pixel_area_m2

EVENT_TYPE = "gnw_forest_disturbance"

# Gundi's event priority scale (gundi-core EventPriority).
PRIORITIES = (0, 100, 200, 300)

DEFAULT_DRIVER_PRIORITY: Dict[str, int] = {
    "mining": 300,
    "selective_logging": 300,
    "road_development": 300,
    "large_scale_agriculture": 200,
    "small_scale_agriculture": 200,
    "wildfire": 100,
    "flooding": 100,
    "landslide": 100,
    "other_natural": 100,
}

CONFIDENCE_LABEL = {1: "nominal", 2: "high", 3: "highest"}

# Driver status of a patch, shown in the title and details.
LABELLED, PENDING, NOT_COVERED = "labelled", "pending", "not_covered"


def _check_priority(value: int, what: str) -> None:
    if value not in PRIORITIES:
        raise ValueError(f"{what} must be one of {PRIORITIES}, got {value!r}")


@dataclass(frozen=True)
class EventConfig:
    """Per-connection choices that shape the event."""
    default_priority: int = 200
    driver_priority_overrides: Mapping[str, int] = field(default_factory=dict)
    event_type: str = EVENT_TYPE
    include_geometry: bool = False

    def __post_init__(self):
        _check_priority(self.default_priority, "default_priority")
        for driver, value in self.driver_priority_overrides.items():
            _check_priority(value, f"priority override for {driver!r}")

    def priority_for(self, driver: Optional[str]) -> int:
        if driver is None:
            return self.default_priority
        if driver in self.driver_priority_overrides:
            return self.driver_priority_overrides[driver]
        return DEFAULT_DRIVER_PRIORITY.get(driver, self.default_priority)


@dataclass(frozen=True)
class DriverInfo:
    """What is known about a patch's driver. `covered` is whether the patch
    lies inside the drivers dataset's extent at all."""
    covered: bool = False
    summary: Optional[DriverSummary] = None
    version: Optional[str] = None
    source: Optional[str] = None

    @property
    def driver(self) -> Optional[str]:
        if not self.covered or self.summary is None or self.summary.driver is None:
            return None
        return str(self.summary.driver)

    @property
    def status(self) -> str:
        if not self.covered:
            return NOT_COVERED
        return LABELLED if self.driver is not None else PENDING


NO_DRIVER = DriverInfo()


def patch_area_m2(patch: Patch, cells_per_degree: int = CELLS_PER_DEGREE) -> float:
    return sum(pixel_area_m2(row, cells_per_degree) for row, _ in patch.pixels)


def _convex_hull(points: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    # Andrew's monotone chain; counter-clockwise, no repeated last point.
    points = sorted(set(points))
    if len(points) <= 2:
        return points

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    def half(seq):
        out: List[Tuple[int, int]] = []
        for p in seq:
            while len(out) >= 2 and cross(out[-2], out[-1], p) <= 0:
                out.pop()
            out.append(p)
        return out[:-1]

    return half(points) + half(reversed(points))


def patch_hull(patch: Patch, cells_per_degree: int = CELLS_PER_DEGREE) -> Optional[dict]:
    """Convex hull of the patch's pixel squares as a GeoJSON Polygon, or None
    for a patch with no pixels. Worked out on integer pixel corners, so the
    ring is exact and a one-pixel patch is that pixel's square."""
    corners = []
    for row, col in patch.pixels:
        # y = -row so that north is up and the ring comes out counter-clockwise.
        for d_row in (0, 1):
            for d_col in (0, 1):
                corners.append((col + d_col, -(row + d_row)))
    ring = _convex_hull(corners)
    if len(ring) < 3:
        return None
    digits = len(str(cells_per_degree)) + 1
    coordinates = [
        [round(x / cells_per_degree - 180.0, digits), round(90.0 + y / cells_per_degree, digits)]
        for x, y in ring
    ]
    coordinates.append(coordinates[0])
    return {"type": "Polygon", "coordinates": [coordinates]}


def event_status(patch: Patch) -> str:
    """ER state for the patch. A closed patch stays active: the disturbance
    has stopped growing, but nobody has dealt with it."""
    return "resolved" if patch.status in (MERGED, WITHDRAWN) else "active"


def _hectares(area_m2: float) -> float:
    return round(area_m2 / 10_000.0, 2)


def _driver_label(driver: str) -> str:
    return driver.replace("_", " ")


def event_title(patch: Patch, driver: DriverInfo = NO_DRIVER) -> str:
    area = f"{_hectares(patch_area_m2(patch)):g} ha"
    if driver.status == LABELLED:
        return f"Forest disturbance: {_driver_label(driver.driver)}, {area}"
    if driver.status == PENDING:
        return f"Forest disturbance: driver pending, {area}"
    return f"Forest disturbance, {area}"


def event_details(
    patch: Patch, driver: DriverInfo = NO_DRIVER, *, in_aoi_area_m2: Optional[float] = None,
) -> dict:
    details = {
        "patch_id": patch.patch_id,
        "patch_status": patch.status,
        "first_alert": patch.first_alert.isoformat() if patch.first_alert else None,
        "last_alert": patch.last_alert.isoformat() if patch.last_alert else None,
        "pixel_count": patch.pixel_count,
        "area_ha": _hectares(patch_area_m2(patch)),
        "max_confidence": CONFIDENCE_LABEL.get(patch.max_confidence),
        "driver_status": driver.status,
        "driver": driver.driver,
        "driver_share": (
            round(driver.summary.share, 2)
            if driver.status == LABELLED and driver.summary.share is not None else None
        ),
        "drivers_version": driver.version if driver.covered else None,
        "driver_source": driver.source if driver.covered else None,
        "merged_into": patch.merged_into,
    }
    if in_aoi_area_m2 is not None:
        details["in_aoi_area_ha"] = _hectares(in_aoi_area_m2)
    return details


def _changeable_fields(patch, config, driver, in_aoi_area_m2) -> dict:
    fields = {
        "title": event_title(patch, driver),
        "status": event_status(patch),
        "priority": config.priority_for(driver.driver),
        "event_details": event_details(patch, driver, in_aoi_area_m2=in_aoi_area_m2),
    }
    if config.include_geometry:
        hull = patch_hull(patch)
        if hull is not None:
            fields["geometry"] = hull
    return fields


def create_payload(
    patch: Patch,
    config: EventConfig = EventConfig(),
    driver: DriverInfo = NO_DRIVER,
    *,
    in_aoi_area_m2: Optional[float] = None,
) -> dict:
    """The event to post the first time a patch is delivered."""
    if patch.first_alert is None:
        raise ValueError(f"Patch {patch.patch_id} has no pixels to report")
    lat, lon = cell_center(*patch.seed)
    recorded_at = datetime.combine(patch.first_alert, time.min, tzinfo=timezone.utc)
    return {
        "event_type": config.event_type,
        "recorded_at": recorded_at.isoformat(),
        # The seed pixel, so the event stays put while the patch grows.
        "location": {"lat": round(lat, 5), "lon": round(lon, 5)},
        **_changeable_fields(patch, config, driver, in_aoi_area_m2),
    }


def update_payload(
    patch: Patch,
    config: EventConfig = EventConfig(),
    driver: DriverInfo = NO_DRIVER,
    *,
    in_aoi_area_m2: Optional[float] = None,
) -> dict:
    """The fields to send on every update: the patch's whole current state,
    never a diff, so an update is safe to repeat and safe to skip. Time and
    location are left out; they are fixed when the event is created."""
    return _changeable_fields(patch, config, driver, in_aoi_area_m2)
