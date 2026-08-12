import pytest
import respx

from app.actions.gnwclient import DataAPI
from app.actions.tests.fields_fixtures import INTEGRATED_FIELDS, VIIRS_FIELDS


@pytest.mark.asyncio
async def test_list_datasets_serves_registry(mocker):
    from app.actions.handlers import action_list_datasets
    from app.actions.configurations import ListDatasetsQuery
    # metadata enrichment failing must not break the action
    mocker.patch.object(DataAPI, "get_datasets", side_effect=Exception("api down"))
    result = await action_list_datasets(integration=None, action_config=ListDatasetsQuery())
    values = {o["value"] for o in result["options"]}
    assert values == {"nasa_viirs_fire_alerts", "gfw_integrated_alerts"}
    labels = {o["value"]: o["label"] for o in result["options"]}
    assert labels["nasa_viirs_fire_alerts"] == "NASA VIIRS Fire Alerts"


@pytest.mark.asyncio
@respx.mock
async def test_list_dataset_fields_filterable_only():
    from app.actions.handlers import action_list_dataset_fields
    from app.actions.configurations import ListDatasetFieldsQuery
    respx.get(f"{DataAPI.DATA_API_URL}/dataset/nasa_viirs_fire_alerts/latest/fields").respond(
        json={"data": [
            {"name": "latitude", "alias": "latitude", "description": None, "data_type": "numeric",
             "unit": None, "is_feature_info": True, "is_filter": False},
            {"name": "frp__MW", "alias": "FRP", "description": "power", "data_type": "numeric",
             "unit": "MW", "is_feature_info": True, "is_filter": True},
        ]}
    )
    result = await action_list_dataset_fields(
        integration=None,
        action_config=ListDatasetFieldsQuery(dataset="nasa_viirs_fire_alerts", filterable_only=True),
    )
    assert [o["value"] for o in result["options"]] == ["frp__MW"]


def test_unknown_dataset_fails_validation():
    import pydantic
    from app.actions.configurations import ListDatasetFieldsQuery
    with pytest.raises(pydantic.ValidationError):
        ListDatasetFieldsQuery(dataset="not_a_dataset")


@pytest.mark.asyncio
@respx.mock
async def test_list_field_values_from_values_table():
    from app.actions.handlers import action_list_field_values
    from app.actions.configurations import ListFieldValuesQuery
    respx.get(f"{DataAPI.DATA_API_URL}/dataset/gfw_integrated_alerts/latest/fields").respond(
        json={"data": INTEGRATED_FIELDS}
    )
    result = await action_list_field_values(
        integration=None,
        action_config=ListFieldValuesQuery(
            dataset="gfw_integrated_alerts", field="gfw_integrated_alerts__confidence"),
    )
    assert [o["value"] for o in result["options"]] == ["nominal", "high", "highest"]
    assert result["options"][0]["description"] == "raster value 2"


@pytest.mark.asyncio
@respx.mock
async def test_list_field_values_falls_back_to_curated_map():
    from app.actions.handlers import action_list_field_values
    from app.actions.configurations import ListFieldValuesQuery
    respx.get(f"{DataAPI.DATA_API_URL}/dataset/nasa_viirs_fire_alerts/latest/fields").respond(
        json={"data": VIIRS_FIELDS}
    )
    result = await action_list_field_values(
        integration=None,
        action_config=ListFieldValuesQuery(
            dataset="nasa_viirs_fire_alerts", field="confidence__cat"),
    )
    assert [(o["value"], o["label"]) for o in result["options"]] == [
        ("h", "high"), ("n", "nominal"), ("l", "low"),
    ]


@pytest.mark.asyncio
@respx.mock
async def test_list_field_values_unknown_field_returns_no_options():
    from app.actions.handlers import action_list_field_values
    from app.actions.configurations import ListFieldValuesQuery
    respx.get(f"{DataAPI.DATA_API_URL}/dataset/nasa_viirs_fire_alerts/latest/fields").respond(
        json={"data": VIIRS_FIELDS}
    )
    result = await action_list_field_values(
        integration=None,
        action_config=ListFieldValuesQuery(dataset="nasa_viirs_fire_alerts", field="nope"),
    )
    assert result["options"] == []


def test_list_field_values_unknown_dataset_fails_validation():
    import pydantic
    from app.actions.configurations import ListFieldValuesQuery
    with pytest.raises(pydantic.ValidationError):
        ListFieldValuesQuery(dataset="not_a_dataset", field="whatever")
