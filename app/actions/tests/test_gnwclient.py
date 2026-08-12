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
