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

    assert {ref["action"] for _, ref in found} == {
        "list_datasets", "list_dataset_fields", "list_field_values",
    }
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


def _resolve_data_ref(rel_path, field_path, root_form_data):
    """Python mirror of the portal's resolveDataRef (gundi-portal
    src/components/common/SchemaFormTemplates/referencePath.ts): start level =
    the annotated field's containing node (drop the last path segment); each
    "../" climbs exactly one data-path segment; remainder is a dotted
    descendant path."""
    climbs = 0
    while rel_path.startswith("../"):
        climbs += 1
        rel_path = rel_path[3:]
    level_length = len(field_path) - 1 - climbs
    if level_length < 0:
        return None
    full_path = list(field_path[:level_length]) + (rel_path.split(".") if rel_path else [])
    current = root_form_data
    for seg in full_path:
        if current is None:
            return None
        try:
            current = current[seg]
        except (KeyError, IndexError, TypeError):
            return None
    return current


def test_gundi_reference_data_paths_resolve_against_portal_semantics():
    """Pin the $data climb counts to the portal's ratified resolver. The
    filter-field and fields-item annotations must resolve to the entry's
    chosen dataset from their respective rjsf field paths."""
    from app.actions.configurations import PullEventsConfig

    form_data = {
        "aoi_url": "https://www.globalnaturewatch.org/dashboards/aoi/abc/",
        "dataset_entries": [
            {"dataset": "nasa_viirs_fire_alerts", "fields": ["frp__MW"],
             "filters": [{"field": "confidence__cat", "operator": "=", "value": "h"}]},
            {"dataset": "gfw_integrated_alerts", "fields": [],
             "filters": [{"field": "", "operator": "=", "value": ""}]},
        ],
    }
    ui = PullEventsConfig.ui_schema()
    entry_items = ui["dataset_entries"]["items"]

    fields_ref = entry_items["fields"]["items"]["gundi:reference"]["params"]["dataset"]["$data"]
    # rjsf field path of dataset_entries[1].fields[0] (a scalar-array item)
    assert _resolve_data_ref(fields_ref, ["dataset_entries", 1, "fields", 0], form_data) \
        == "gfw_integrated_alerts"

    filter_ref = entry_items["filters"]["items"]["field"]["gundi:reference"]["params"]["dataset"]["$data"]
    # rjsf field path of dataset_entries[0].filters[0].field
    assert _resolve_data_ref(filter_ref, ["dataset_entries", 0, "filters", 0, "field"], form_data) \
        == "nasa_viirs_fire_alerts"
    # and per-entry isolation: the second entry's filter resolves its own dataset
    assert _resolve_data_ref(filter_ref, ["dataset_entries", 1, "filters", 0, "field"], form_data) \
        == "gfw_integrated_alerts"

    value_params = entry_items["filters"]["items"]["value"]["gundi:reference"]["params"]
    dataset_ref = value_params["dataset"]["$data"]
    field_ref = value_params["field"]["$data"]
    # rjsf field path of dataset_entries[0].filters[0].value: dataset via 2 climbs
    assert _resolve_data_ref(dataset_ref, ["dataset_entries", 0, "filters", 0, "value"], form_data) \
        == "nasa_viirs_fire_alerts"
    # field with zero climbs = sibling within the same FilterRow
    assert _resolve_data_ref(field_ref, ["dataset_entries", 0, "filters", 0, "value"], form_data) \
        == "confidence__cat"
