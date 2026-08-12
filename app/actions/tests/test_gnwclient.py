import pytest
import httpx
import respx
from datetime import datetime, timedelta, timezone

from app.actions.gnwclient import DataAPI
from app.actions.tests.fixtures import TOKEN_PAYLOAD, api_key_item


@pytest.fixture
def fast_backoff(monkeypatch):
    import asyncio
    real_sleep = asyncio.sleep
    async def instant(_secs, *a, **k):
        await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", instant)


@pytest.mark.asyncio
@respx.mock
async def test_get_access_token():
    respx.post(f"{DataAPI.DATA_API_URL}/auth/token").respond(json=TOKEN_PAYLOAD)
    client = DataAPI(username="user@example.com", password="pw")
    token = await client.get_access_token()
    assert token.access_token == "tok123"
    header = await client.get_auth_header()
    assert header == {"authorization": "bearer tok123"}


@pytest.mark.asyncio
@respx.mock
async def test_get_a_valid_api_key_reuses_unexpired_key():
    respx.post(f"{DataAPI.DATA_API_URL}/auth/token").respond(json=TOKEN_PAYLOAD)
    respx.get(f"{DataAPI.DATA_API_URL}/auth/apikeys").respond(
        json={"data": [api_key_item(expires_delta_days=-1, key="expired"), api_key_item(key="good")]}
    )
    client = DataAPI(username="user@example.com", password="pw")
    key = await client.get_a_valid_api_key()
    assert key.api_key == "good"


@pytest.mark.asyncio
@respx.mock
async def test_get_a_valid_api_key_creates_when_none_valid(fast_backoff):
    respx.post(f"{DataAPI.DATA_API_URL}/auth/token").respond(json=TOKEN_PAYLOAD)
    respx.get(f"{DataAPI.DATA_API_URL}/auth/apikeys").respond(status_code=404)
    respx.post(f"{DataAPI.DATA_API_URL}/auth/apikey").respond(json={"data": api_key_item(key="fresh")})
    client = DataAPI(username="user@example.com", password="pw")
    key = await client.get_a_valid_api_key()
    assert key.api_key == "fresh"


@pytest.mark.asyncio
@respx.mock
async def test_aoi_from_url_parses_both_domains():
    client = DataAPI(username="u", password="p")
    assert await client.aoi_from_url("https://www.globalforestwatch.org/dashboards/aoi/abc123/?x=1") == "abc123"
    assert await client.aoi_from_url("https://www.globalnaturewatch.org/dashboards/aoi/def456") == "def456"


@pytest.mark.asyncio
@respx.mock
async def test_aoi_from_url_follows_short_link_redirect():
    respx.head("https://gfw.global/3VVYy6f").respond(
        status_code=301, headers={"location": "https://www.globalnaturewatch.org/dashboards/aoi/xyz789"}
    )
    respx.head("https://www.globalnaturewatch.org/dashboards/aoi/xyz789").respond(status_code=200)
    client = DataAPI(username="u", password="p")
    assert await client.aoi_from_url("https://gfw.global/3VVYy6f") == "xyz789"


@pytest.mark.asyncio
@respx.mock
async def test_get_dataset_fields():
    respx.get(f"{DataAPI.DATA_API_URL}/dataset/nasa_viirs_fire_alerts/latest/fields").respond(
        json={"data": [
            {"name": "alert__date", "alias": "date", "description": None,
             "data_type": "date", "unit": None, "is_feature_info": True, "is_filter": True},
            {"name": "frp__MW", "alias": "FRP", "description": "fire radiative power",
             "data_type": "numeric", "unit": "MW", "is_feature_info": True, "is_filter": True},
        ]}
    )
    client = DataAPI(username="u", password="p")
    fields = await client.get_dataset_fields(dataset="nasa_viirs_fire_alerts")
    assert [f.name for f in fields] == ["alert__date", "frp__MW"]
    assert fields[1].is_filter is True


@pytest.mark.asyncio
@respx.mock
async def test_get_aoi_and_geostore():
    respx.post(f"{DataAPI.DATA_API_URL}/auth/token").respond(json=TOKEN_PAYLOAD)
    respx.get(f"{DataAPI.RESOURCE_WATCH_URL}/v2/area/abc123").respond(json={"data": {
        "type": "area", "id": "abc123",
        "attributes": {"name": "My AOI", "application": "gfw", "geostore": "geo-1",
                       "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z",
                       "use": {}, "env": "production", "tags": [], "status": "saved", "public": True},
    }})
    client = DataAPI(username="u", password="p")
    aoi = await client.get_aoi(aoi_id="abc123")
    assert aoi.attributes.geostore == "geo-1"
