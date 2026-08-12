import pytest
from app.actions.configurations import (
    DatasetEntry, FilterRow, H3GridOutput, PerRecordOutput, PullEventsConfig, entry_state_key,
)
from app.actions.datasets import DATASET_REGISTRY


def test_dataset_entry_defaults_to_per_record():
    entry = DatasetEntry(dataset="nasa_viirs_fire_alerts")
    assert entry.output.mode == "per_record"


def test_output_union_discriminates_on_mode():
    entry = DatasetEntry.parse_obj({"dataset": "x", "output": {"mode": "h3_grid", "resolution": 8}})
    assert isinstance(entry.output, H3GridOutput) and entry.output.resolution == 8


def test_h3_resolution_bounds():
    with pytest.raises(Exception):
        DatasetEntry.parse_obj({"dataset": "x", "output": {"mode": "h3_grid", "resolution": 12}})


def test_resolved_event_type_appends_agg_suffix_for_h3():
    spec = DATASET_REGISTRY["nasa_viirs_fire_alerts"]
    per_record = DatasetEntry(dataset="nasa_viirs_fire_alerts")
    h3_entry = DatasetEntry(dataset="nasa_viirs_fire_alerts", output=H3GridOutput())
    custom = DatasetEntry(dataset="nasa_viirs_fire_alerts", event_type="my_type")
    assert per_record.resolved_event_type(spec) == "gnw_viirs_fires"
    assert h3_entry.resolved_event_type(spec) == "gnw_viirs_fires_agg"
    assert custom.resolved_event_type(spec) == "my_type"


def test_entry_state_key_stable_and_distinct():
    e1 = DatasetEntry(dataset="a", filters=[FilterRow(field="f", operator="=", value="1"),
                                            FilterRow(field="g", operator=">", value="2")])
    e1_reordered = DatasetEntry(dataset="a", filters=[FilterRow(field="g", operator=">", value="2"),
                                                      FilterRow(field="f", operator="=", value="1")])
    e2 = DatasetEntry(dataset="a", filters=[FilterRow(field="f", operator="=", value="9")])
    assert entry_state_key(e1) == entry_state_key(e1_reordered)  # filter order irrelevant
    assert entry_state_key(e1) != entry_state_key(e2)
    assert len(entry_state_key(e1)) == 16


def test_pull_events_config_parses():
    config = PullEventsConfig.parse_obj({
        "aoi_url": "https://www.globalnaturewatch.org/dashboards/aoi/abc/",
        "dataset_entries": [{"dataset": "nasa_viirs_fire_alerts"}],
    })
    assert config.dataset_entries[0].dataset == "nasa_viirs_fire_alerts"
    assert config.force_fetch is False
