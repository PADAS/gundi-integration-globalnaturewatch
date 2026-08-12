import datetime
import pytest

from app.actions.datasets import DATASET_REGISTRY
from app.actions.gnwclient import DatasetField
from app.actions.sqlbuilder import build_query, render_literal, ConfigValidationError


def make_fields(*specs):
    return [DatasetField(name=n, alias=n, description=None, data_type=t, unit=None,
                         is_feature_info=True, is_filter=f) for n, t, f in specs]

VIIRS_SPEC = DATASET_REGISTRY["nasa_viirs_fire_alerts"]
FIELDS = make_fields(
    ("latitude", "numeric", False), ("longitude", "numeric", False),
    ("alert__date", "date", True), ("confidence__cat", "text", True),
    ("frp__MW", "numeric", True),
)
WINDOW = (datetime.date(2026, 8, 1), datetime.date(2026, 8, 8))


def test_build_query_happy_path():
    sql = build_query(spec=VIIRS_SPEC, extra_fields=[], filters=[
        {"field": "confidence__cat", "operator": "=", "value": "h"},
        {"field": "frp__MW", "operator": ">", "value": "5.5"},
    ], window_start=WINDOW[0], window_end=WINDOW[1], dataset_fields=FIELDS)
    # SELECT is deterministic: lat, lon, date_field, then defaults+extras in listed order
    assert sql == (
        "SELECT latitude,longitude,alert__date,confidence__cat,frp__MW FROM results"
        " WHERE (alert__date >= '2026-08-01' AND alert__date <= '2026-08-08')"
        " AND confidence__cat = 'h' AND frp__MW > 5.5"
    )


def test_unknown_identifiers_rejected():
    with pytest.raises(ConfigValidationError) as exc:
        build_query(spec=VIIRS_SPEC, extra_fields=["nope__col"], filters=[],
                    window_start=WINDOW[0], window_end=WINDOW[1], dataset_fields=FIELDS)
    assert "nope__col" in str(exc.value)


def test_non_filterable_field_rejected_in_filters():
    with pytest.raises(ConfigValidationError):
        build_query(spec=VIIRS_SPEC, extra_fields=[], filters=[
            {"field": "latitude", "operator": ">", "value": "0"}],
            window_start=WINDOW[0], window_end=WINDOW[1], dataset_fields=FIELDS)


def test_hostile_string_value_is_escaped_not_injected():
    sql = build_query(spec=VIIRS_SPEC, extra_fields=[], filters=[
        {"field": "confidence__cat", "operator": "=", "value": "h' OR '1'='1"}],
        window_start=WINDOW[0], window_end=WINDOW[1], dataset_fields=FIELDS)
    assert "confidence__cat = 'h'' OR ''1''=''1'" in sql


def test_numeric_value_must_parse():
    with pytest.raises(ConfigValidationError):
        build_query(spec=VIIRS_SPEC, extra_fields=[], filters=[
            {"field": "frp__MW", "operator": ">", "value": "5; DROP TABLE"}],
            window_start=WINDOW[0], window_end=WINDOW[1], dataset_fields=FIELDS)


def test_in_operator_renders_list():
    sql = build_query(spec=VIIRS_SPEC, extra_fields=[], filters=[
        {"field": "confidence__cat", "operator": "in", "value": "h, n"}],
        window_start=WINDOW[0], window_end=WINDOW[1], dataset_fields=FIELDS)
    assert "confidence__cat IN ('h','n')" in sql


def test_render_literal_types():
    assert render_literal("5.5", "numeric") == "5.5"
    assert render_literal("7", "integer") == "7"
    assert render_literal("true", "boolean") == "TRUE"
    assert render_literal("2026-08-01", "date") == "'2026-08-01'"
    assert render_literal("it's", "text") == "'it''s'"


def test_render_literal_unknown_type_parses_or_quotes():
    assert render_literal("5.5", None) == "5.5"
    assert render_literal("high", None) == "'high'"
    assert render_literal("5; DROP TABLE x", None) == "'5; DROP TABLE x'"


def test_filter_on_raster_field_with_null_data_type():
    fields = make_fields(
        ("latitude", "numeric", False), ("longitude", "numeric", False),
        ("alert__date", "date", True), ("confidence__cat", "text", True),
        ("frp__MW", "numeric", True),
    ) + [
        DatasetField.parse_obj({"pixel_meaning": "gfw_integrated_alerts__confidence", "unit": None,
                                "description": None, "statistics": None, "values_table": None,
                                "data_type": None, "compression": None, "no_data_value": None})]
    sql = build_query(spec=VIIRS_SPEC, extra_fields=[], filters=[
        {"field": "gfw_integrated_alerts__confidence", "operator": "=", "value": "high"}],
        window_start=WINDOW[0], window_end=WINDOW[1], dataset_fields=fields)
    assert "gfw_integrated_alerts__confidence = 'high'" in sql


def test_missing_spec_default_field_rejected():
    fields = make_fields(("latitude", "numeric", False), ("longitude", "numeric", False),
                         ("alert__date", "date", True), ("confidence__cat", "text", True))
    # frp__MW (a spec default) is absent from the dataset's live inventory
    with pytest.raises(ConfigValidationError) as exc:
        build_query(spec=VIIRS_SPEC, extra_fields=[], filters=[],
                    window_start=WINDOW[0], window_end=WINDOW[1], dataset_fields=fields)
    assert "frp__MW" in str(exc.value)


def test_multiple_bad_filters_accumulate_errors():
    with pytest.raises(ConfigValidationError) as exc:
        build_query(spec=VIIRS_SPEC, extra_fields=[], filters=[
            {"field": "latitude", "operator": ">", "value": "0"},
            {"field": "frp__MW", "operator": ">", "value": "5; DROP TABLE"},
        ], window_start=WINDOW[0], window_end=WINDOW[1], dataset_fields=FIELDS)
    assert len(exc.value.errors) == 2
