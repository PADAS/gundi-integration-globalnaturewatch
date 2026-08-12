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


def _collect_gundi_references(node, found):
    if isinstance(node, dict):
        if "gundi:reference" in node:
            found.append((node, node["gundi:reference"]))
        for value in node.values():
            _collect_gundi_references(value, found)


def test_gundi_reference_annotations_match_registered_reference_actions():
    from app.actions.configurations import PullEventsConfig
    from app.actions.core import ReferenceActionConfiguration, discover_actions

    handlers = discover_actions(module_name="app.actions.handlers", prefix="action_")
    found = []
    _collect_gundi_references(PullEventsConfig.ui_schema(), found)

    assert {ref["action"] for _, ref in found} == {"list_datasets", "list_dataset_fields"}
    for host_node, ref in found:
        assert ref["target"] == "self"
        assert "ui:widget" not in host_node, ref["action"]
        assert ref["allow_free_text"] is True
        _, config_model, _ = handlers[ref["action"]]
        assert issubclass(config_model, ReferenceActionConfiguration)
        declared = {k for k, v in ref.get("params", {}).items() if not isinstance(v, dict)}
        data_bound = {k for k, v in ref.get("params", {}).items() if isinstance(v, dict)}
        model_fields = set(config_model.__fields__)
        assert (declared | data_bound) <= model_fields
        required = {n for n, f in config_model.__fields__.items() if f.required}
        assert required <= (declared | data_bound)
