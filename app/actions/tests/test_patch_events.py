from datetime import date

import pytest

from app.actions.patches.core import CLOSED, MERGED, OPEN, WITHDRAWN, Patch, Pixel
from app.actions.patches.drivers import summarise
from app.actions.patches.events import (
    DriverInfo, EventConfig, create_payload, event_status, patch_area_m2, patch_hull,
    update_payload,
)
from app.actions.patches.grid import cell_center, cell_index, pixel_area_m2

# A cell near the equator in the Congo Basin.
ROW, COL = cell_index(-1.23455, 23.45675)


def make_patch(cells, status=OPEN, merged_into=None, confidence=2,
               first=date(2026, 7, 1), last=date(2026, 7, 12)):
    dates = [first] + [last] * (len(cells) - 1)
    patch = Patch(patch_id="patch-1", created_order=0, seed=cells[0], status=status,
                  merged_into=merged_into)
    patch.pixels = {cell: Pixel(cell[0], cell[1], d, confidence) for cell, d in zip(cells, dates)}
    patch.refresh()
    return patch


def block(rows, cols):
    return [(ROW + r, COL + c) for r in range(rows) for c in range(cols)]


def labelled(*classes, version="v20261019"):
    return DriverInfo(covered=True, summary=summarise(classes), version=version, source="gnw")


def test_create_payload_for_a_patch_outside_drivers_coverage():
    patch = make_patch(block(2, 3))
    event = create_payload(patch)

    lat, lon = cell_center(ROW, COL)
    assert event["event_type"] == "gnw_forest_disturbance"
    assert event["recorded_at"] == "2026-07-01T00:00:00+00:00"
    assert event["location"] == {"lat": round(lat, 5), "lon": round(lon, 5)}
    assert event["status"] == "active"
    assert event["priority"] == 200
    assert event["title"] == "Forest disturbance, 0.07 ha"
    assert "geometry" not in event
    assert event["event_details"] == {
        "patch_id": "patch-1",
        "patch_status": "open",
        "first_alert": "2026-07-01",
        "last_alert": "2026-07-12",
        "pixel_count": 6,
        "area_ha": 0.07,
        "max_confidence": "high",
        "driver_status": "not_covered",
        "driver": None,
        "driver_share": None,
        "drivers_version": None,
        "driver_source": None,
        "merged_into": None,
    }


def test_labelled_patch_carries_the_driver_and_its_priority():
    patch = make_patch(block(2, 2))
    event = create_payload(patch, driver=labelled("mining", "mining", "mining", "wildfire"))

    assert event["title"] == "Forest disturbance: mining, 0.05 ha"
    assert event["priority"] == 300
    details = event["event_details"]
    assert details["driver_status"] == "labelled"
    assert details["driver"] == "mining"
    assert details["driver_share"] == 0.75
    assert details["drivers_version"] == "v20261019"
    assert details["driver_source"] == "gnw"


def test_covered_patch_without_labels_yet_is_pending_at_default_priority():
    patch = make_patch(block(1, 2))
    event = create_payload(
        patch, EventConfig(default_priority=100),
        DriverInfo(covered=True, summary=summarise([None, None]), version="v20261019"),
    )
    assert event["title"] == "Forest disturbance: driver pending, 0.02 ha"
    assert event["priority"] == 100
    assert event["event_details"]["driver_status"] == "pending"
    assert event["event_details"]["driver"] is None


def test_driver_name_with_underscores_reads_naturally_in_the_title():
    patch = make_patch(block(1, 1))
    event = create_payload(patch, driver=labelled("selective_logging"))
    assert event["title"].startswith("Forest disturbance: selective logging, ")


@pytest.mark.parametrize("driver, expected", [
    ("mining", 300), ("road_development", 300), ("large_scale_agriculture", 200),
    ("wildfire", 100), ("landslide", 100),
])
def test_default_driver_priorities(driver, expected):
    assert EventConfig().priority_for(driver) == expected


def test_priority_override_wins_and_unknown_class_gets_the_default():
    config = EventConfig(default_priority=0, driver_priority_overrides={"wildfire": 300})
    assert config.priority_for("wildfire") == 300
    assert config.priority_for("mining") == 300          # untouched default mapping
    assert config.priority_for("a_class_gnw_adds_later") == 0
    assert config.priority_for(None) == 0


@pytest.mark.parametrize("kwargs", [
    {"default_priority": 150},
    {"default_priority": None},
    {"driver_priority_overrides": {"mining": 250}},
])
def test_config_rejects_a_priority_gundi_would_refuse(kwargs):
    with pytest.raises(ValueError):
        EventConfig(**kwargs)


@pytest.mark.parametrize("status, expected", [
    (OPEN, "active"), (CLOSED, "active"), (MERGED, "resolved"), (WITHDRAWN, "resolved"),
])
def test_event_status(status, expected):
    assert event_status(make_patch(block(1, 1), status=status)) == expected


def test_merged_patch_is_resolved_and_points_at_the_survivor():
    patch = make_patch(block(1, 2), status=MERGED, merged_into="patch-0")
    update = update_payload(patch)
    assert update["status"] == "resolved"
    assert update["event_details"]["patch_status"] == "merged"
    assert update["event_details"]["merged_into"] == "patch-0"


def test_update_payload_never_moves_the_event_in_time_or_place():
    patch = make_patch(block(2, 2))
    update = update_payload(patch, driver=labelled("mining"))
    assert set(update) == {"title", "status", "priority", "event_details"}


def test_update_is_the_same_fields_the_create_carried():
    patch = make_patch(block(2, 2))
    driver = labelled("wildfire")
    created = create_payload(patch, driver=driver, in_aoi_area_m2=123.0)
    update = update_payload(patch, driver=driver, in_aoi_area_m2=123.0)
    assert update == {k: created[k] for k in update}


def test_in_aoi_area_is_reported_only_when_known():
    patch = make_patch(block(2, 2))
    assert "in_aoi_area_ha" not in create_payload(patch)["event_details"]
    event = create_payload(patch, in_aoi_area_m2=patch_area_m2(patch) / 2)
    assert event["event_details"]["in_aoi_area_ha"] == pytest.approx(0.02, abs=0.01)


def test_patch_without_pixels_cannot_be_created():
    patch = Patch(patch_id="empty", created_order=0, seed=(ROW, COL))
    with pytest.raises(ValueError):
        create_payload(patch)


def test_withdrawn_patch_that_lost_all_pixels_still_updates():
    patch = make_patch(block(1, 1), status=WITHDRAWN)
    patch.pixels = {}
    update = update_payload(patch)
    assert update["status"] == "resolved"
    assert update["event_details"]["pixel_count"] == 0
    assert update["event_details"]["first_alert"] == "2026-07-01"


def test_area_is_latitude_corrected():
    patch = make_patch(block(3, 3))
    assert patch_area_m2(patch) == pytest.approx(
        sum(3 * pixel_area_m2(ROW + r) for r in range(3)))
    polar = Patch(patch_id="north", created_order=0, seed=(300_000, COL))
    polar.pixels = {(300_000, COL): Pixel(300_000, COL, date(2026, 7, 1), 1)}
    assert patch_area_m2(polar) < pixel_area_m2(ROW)


def test_hull_of_one_pixel_is_that_pixels_square():
    hull = patch_hull(make_patch([(ROW, COL)]))
    lat, lon = cell_center(ROW, COL)
    ring = hull["coordinates"][0]
    assert hull["type"] == "Polygon"
    assert ring[0] == ring[-1] and len(ring) == 5
    assert sorted({p[0] for p in ring}) == pytest.approx([lon - 0.00005, lon + 0.00005])
    assert sorted({p[1] for p in ring}) == pytest.approx([lat - 0.00005, lat + 0.00005])


def test_hull_is_counter_clockwise_and_contains_every_pixel_centre():
    cells = [(ROW, COL), (ROW + 1, COL + 1), (ROW + 2, COL + 2), (ROW + 2, COL), (ROW + 4, COL + 1)]
    ring = patch_hull(make_patch(cells))["coordinates"][0]

    signed_area = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:]))
    assert signed_area > 0   # GeoJSON exterior rings run counter-clockwise

    def inside(lon, lat):
        return all(
            (x2 - x1) * (lat - y1) - (y2 - y1) * (lon - x1) >= -1e-12
            for (x1, y1), (x2, y2) in zip(ring, ring[1:])
        )
    for cell in cells:
        lat, lon = cell_center(*cell)
        assert inside(lon, lat)


def test_hull_of_a_straight_run_has_no_redundant_points():
    ring = patch_hull(make_patch([(ROW, COL + c) for c in range(10)]))["coordinates"][0]
    assert len(ring) == 5


def test_geometry_is_sent_only_when_enabled():
    patch = make_patch(block(2, 2))
    config = EventConfig(include_geometry=True)
    assert create_payload(patch, config)["geometry"] == patch_hull(patch)
    assert update_payload(patch, config)["geometry"] == patch_hull(patch)
    assert "geometry" not in update_payload(patch)
