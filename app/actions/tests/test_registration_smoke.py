import json


def test_discover_actions_finds_expected_handlers():
    from app.actions.core import discover_actions
    handlers = discover_actions(module_name="app.actions.handlers", prefix="action_")
    assert set(handlers) == {
        "auth", "pull_events", "run_query_job", "list_datasets", "list_dataset_fields",
        "reset_quiet_periods",
    }


def test_portal_schemas_serialize():
    """Every portal-registered config must produce valid JSON schema + ui_schema."""
    from app.actions.configurations import (
        AuthenticateConfig, PullEventsConfig, ResetQuietPeriodsConfig,
    )
    for model in (AuthenticateConfig, PullEventsConfig, ResetQuietPeriodsConfig):
        schema = json.loads(model.schema_json())
        assert schema["title"]
        assert isinstance(model.ui_schema(), dict)

    pull_schema = json.loads(PullEventsConfig.schema_json())

    # dataset_entries is an array of DatasetEntry (via $ref into definitions)
    entry_schema_ref = pull_schema["properties"]["dataset_entries"]
    assert entry_schema_ref["type"] == "array"
    assert entry_schema_ref["items"]["$ref"] == "#/definitions/DatasetEntry"

    definitions = pull_schema.get("definitions", {})
    assert "H3GridOutput" in definitions
    assert "PerRecordOutput" in definitions

    # H3GridOutput must carry the resolution field users pick a grid size with.
    h3_props = definitions["H3GridOutput"]["properties"]
    assert h3_props["resolution"]["type"] == "integer"
    assert h3_props["resolution"]["minimum"] == 4
    assert h3_props["resolution"]["maximum"] == 10

    # DatasetEntry.output renders as a "oneOf" of $refs — what
    # react-jsonschema-form uses for the per_record/h3_grid mode dropdown.
    output_schema = definitions["DatasetEntry"]["properties"]["output"]
    one_of_refs = {branch["$ref"] for branch in output_schema["oneOf"]}
    assert one_of_refs == {
        "#/definitions/PerRecordOutput",
        "#/definitions/H3GridOutput",
    }

    # The OpenAPI "discriminator" keyword pydantic v1 emits for the union must
    # be stripped EVERYWHERE: the portal's react-jsonschema-form runs ajv in
    # strict mode, which refuses to compile schemas containing unknown
    # keywords ("strict mode: unknown keyword: discriminator") and the config
    # form cannot validate or save. Runtime parsing is unaffected — pydantic
    # discriminates via the Field, not the emitted schema.
    def find_keyword(node, keyword):
        hits = []
        if isinstance(node, dict):
            for k, v in node.items():
                if k == keyword:
                    hits.append(v)
                hits += find_keyword(v, keyword)
        elif isinstance(node, list):
            for v in node:
                hits += find_keyword(v, keyword)
        return hits

    assert find_keyword(pull_schema, "discriminator") == []


def test_run_query_job_is_internal():
    from app.actions.configurations import RunQueryJobConfig
    from app.actions.core import InternalActionConfiguration
    assert issubclass(RunQueryJobConfig, InternalActionConfiguration)


def test_pull_events_has_crontab():
    from app.actions.handlers import action_pull_events
    schedule = getattr(action_pull_events, "crontab_schedule", None)
    assert schedule is not None
    rendered = (
        f"{schedule.minute} {schedule.hour} "
        f"{schedule.day_of_month} {schedule.month_of_year} {schedule.day_of_week}"
    )
    assert rendered == "*/10 * * * *"
