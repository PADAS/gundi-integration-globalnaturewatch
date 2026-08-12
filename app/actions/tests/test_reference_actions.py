import pytest
import respx

from app.actions.gnwclient import DataAPI


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
