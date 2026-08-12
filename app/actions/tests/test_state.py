import json
import pytest
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from app.actions.state import GnwState
from app.actions.tests.fixtures import StubPipeline, stub_state_manager  # noqa: F401


@pytest.mark.asyncio
async def test_aoi_cache_roundtrip(stub_state_manager):
    state = GnwState(stub_state_manager)
    await state.set_aoi_data("int-1", {"id": "aoi-1"})
    assert await state.get_aoi_data("int-1") == {"id": "aoi-1"}
    assert await state.get_aoi_data("int-2") is None


@pytest.mark.asyncio
async def test_quiet_period(stub_state_manager):
    state = GnwState(stub_state_manager)
    assert not await state.is_quiet_period("int-1", "abc123")
    await state.set_quiet_period("int-1", "abc123", timedelta(minutes=30))
    assert await state.is_quiet_period("int-1", "abc123")
    assert not await state.is_quiet_period("int-1", "other")


@pytest.mark.asyncio
async def test_set_quiet_period_zero_is_noop(stub_state_manager):
    state = GnwState(stub_state_manager)
    await state.set_quiet_period("int-1", "abc123", timedelta(minutes=0))
    assert not await state.is_quiet_period("int-1", "abc123")


@pytest.mark.asyncio
async def test_pending_jobs_roundtrip_and_expiry_cleanup(stub_state_manager):
    state = GnwState(stub_state_manager)
    await state.add_pending_job("int-1", "abc123", {"job_id": "j1", "job_link": "https://x/j1"})
    jobs = await state.get_pending_jobs("int-1", "abc123")
    assert jobs == [{"job_id": "j1", "job_link": "https://x/j1"}]
    # simulate the job value expiring while the set still references it
    del stub_state_manager._store[state._pending_job_key("int-1", "abc123", "j1")]
    assert await state.get_pending_jobs("int-1", "abc123") == []
    await state.remove_pending_job("int-1", "abc123", "j1")  # idempotent


@pytest.mark.asyncio
async def test_window_anchor_roundtrip(stub_state_manager):
    state = GnwState(stub_state_manager)
    assert await state.get_window_anchor("int-1", "abc123") is None
    await state.set_window_anchor("int-1", "abc123", "2026-08-01")
    assert await state.get_window_anchor("int-1", "abc123") == "2026-08-01"
