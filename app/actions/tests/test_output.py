import h3
from app.actions.configurations import DatasetEntry, H3GridOutput
from app.actions.datasets import DATASET_REGISTRY
from app.actions.output import OUTPUT_STRATEGIES, row_fingerprint

SPEC = DATASET_REGISTRY["nasa_viirs_fire_alerts"]

ROWS = [
    {"latitude": -1.2801, "longitude": 36.8167, "alert__date": "2026-08-05",
     "confidence__cat": "h", "frp__MW": 10.0},
    {"latitude": -1.2802, "longitude": 36.8168, "alert__date": "2026-08-06",
     "confidence__cat": "n", "frp__MW": 30.0},
    {"latitude": 40.0, "longitude": -105.0, "alert__date": "2026-08-04",
     "confidence__cat": "h", "frp__MW": 2.0},
]


def test_per_record_emits_one_event_per_row():
    entry = DatasetEntry(dataset="nasa_viirs_fire_alerts")
    events = OUTPUT_STRATEGIES["per_record"].to_events(ROWS, entry, SPEC)
    assert len(events) == 3
    event = events[0]
    assert event["event_type"] == "gnw_viirs_fires"
    assert event["title"] == "NASA VIIRS Fire Alerts"
    assert event["location"] == {"lat": -1.2801, "lon": 36.8167}
    assert event["recorded_at"] == "2026-08-05T00:00:00+00:00"
    assert event["event_details"]["frp__MW"] == 10.0
    assert event["event_details"]["confidence__cat"] == "h"


def test_h3_grid_buckets_and_aggregates():
    entry = DatasetEntry(dataset="nasa_viirs_fire_alerts", output=H3GridOutput(resolution=7))
    events = OUTPUT_STRATEGIES["h3_grid"].to_events(ROWS, entry, SPEC)
    # first two rows are ~15m apart -> same res-7 cell; third is far away
    assert len(events) == 2
    big = next(e for e in events if e["event_details"]["record_count"] == 2)
    assert big["event_type"] == "gnw_viirs_fires_agg"
    assert big["title"] == "NASA VIIRS Fire Alerts — 2 records"
    assert big["recorded_at"] == "2026-08-06T00:00:00+00:00"  # newest in cell
    details = big["event_details"]
    assert details["resolution"] == 7
    assert details["frp__MW_min"] == 10.0 and details["frp__MW_max"] == 30.0
    assert details["frp__MW_mean"] == 20.0
    assert "confidence__cat_mean" not in details  # non-numeric skipped
    expected_cell = h3.latlng_to_cell(-1.2801, 36.8167, 7)
    assert details["cell_id"] == expected_cell
    lat, lon = h3.cell_to_latlng(expected_cell)
    assert abs(big["location"]["lat"] - lat) < 1e-9 and abs(big["location"]["lon"] - lon) < 1e-9


def test_h3_grid_empty_rows():
    entry = DatasetEntry(dataset="nasa_viirs_fire_alerts", output=H3GridOutput())
    assert OUTPUT_STRATEGIES["h3_grid"].to_events([], entry, SPEC) == []


def test_row_fingerprint_stable_and_order_insensitive():
    row = {"a": 1, "b": "x", "c": 2.5}
    reordered = {"c": 2.5, "a": 1, "b": "x"}
    assert row_fingerprint(row) == row_fingerprint(reordered)


def test_row_fingerprint_changes_on_value_change():
    base = {"a": 1, "confidence__cat": "n"}
    upgraded = {"a": 1, "confidence__cat": "h"}
    assert row_fingerprint(base) != row_fingerprint(upgraded)
