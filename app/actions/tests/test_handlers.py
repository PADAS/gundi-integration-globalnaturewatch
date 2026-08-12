import datetime
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.actions.configurations import DatasetEntry, RunQueryJobConfig
from app.actions.gnwclient import DataAPI, DownloadLinkExpiredException, JobResponse
from app.actions.tests.fixtures import stub_state_manager  # reuse the stub fixture

FIELDS_JSON = {"data": [
    {"name": "latitude", "alias": "latitude", "description": None, "data_type": "numeric",
     "unit": None, "is_feature_info": True, "is_filter": False},
    {"name": "longitude", "alias": "longitude", "description": None, "data_type": "numeric",
     "unit": None, "is_feature_info": True, "is_filter": False},
    {"name": "alert__date", "alias": "date", "description": None, "data_type": "date",
     "unit": None, "is_feature_info": True, "is_filter": True},
    {"name": "confidence__cat", "alias": "confidence", "description": None, "data_type": "text",
     "unit": None, "is_feature_info": True, "is_filter": True},
    {"name": "frp__MW", "alias": "FRP", "description": None, "data_type": "numeric",
     "unit": "MW", "is_feature_info": True, "is_filter": True},
    {"name": "gfw_integrated_alerts__date", "alias": "date", "description": None, "data_type": "date",
     "unit": None, "is_feature_info": True, "is_filter": True},
    {"name": "gfw_integrated_alerts__confidence", "alias": "confidence", "description": None,
     "data_type": "text", "unit": None, "is_feature_info": True, "is_filter": True},
]}


@pytest.fixture
def integration_v2_like():
    integration = MagicMock()
    integration.id = "579b2f56-1234-5678-9abc-def012345678"
    auth_config = MagicMock()
    auth_config.data = {"email": "u@example.com", "password": "pw"}
    integration.configurations = [auth_config]
    return integration


@pytest.fixture
def handler_env(mocker, stub_state_manager, integration_v2_like):
    """Patch handlers' module-level collaborators."""
    import app.actions.handlers as handlers
    from app.actions.state import GnwState
    mocker.patch.object(handlers, "state_manager", stub_state_manager)
    mocker.patch.object(handlers, "gnw_state", GnwState(stub_state_manager))
    send = mocker.patch.object(handlers, "send_events_to_gundi", AsyncMock(return_value=[]))
    mocker.patch.object(handlers, "get_auth_config",
                        return_value=MagicMock(email="u@example.com",
                                               password=MagicMock(get_secret_value=lambda: "pw")))
    mocker.patch.object(handlers, "log_action_activity", AsyncMock())
    mocker.patch.object(DataAPI, "get_dataset_fields", AsyncMock(
        return_value=[__import__("app.actions.gnwclient", fromlist=["DatasetField"]).DatasetField.parse_obj(f)
                      for f in FIELDS_JSON["data"]]))
    return handlers, send, stub_state_manager, integration_v2_like


@pytest.mark.asyncio
async def test_run_query_job_sync_posts_events_and_advances_anchor(mocker, handler_env):
    handlers, send, state_stub, integration = handler_env
    mocker.patch.object(DataAPI, "query_sync", AsyncMock(return_value=[
        {"latitude": 1.0, "longitude": 2.0, "alert__date": "2026-08-05",
         "confidence__cat": "h", "frp__MW": 3.0},
    ]))
    from app.actions.configurations import entry_state_key
    entry = DatasetEntry(dataset="nasa_viirs_fire_alerts")
    key = entry_state_key(entry)
    await handlers.gnw_state.set_window_anchor(str(integration.id), key, "2026-08-01")

    config = RunQueryJobConfig(entry=entry, geostore_ids=["geo-1"],
                               window_start=datetime.date(2026, 8, 1),
                               window_end=datetime.date(2026, 8, 8))
    result = await handlers.action_run_query_job(integration, config)

    assert result["events_posted"] == 1
    posted = send.call_args.kwargs["events"]
    assert posted[0]["event_type"] == "gnw_viirs_fires"
    assert await handlers.gnw_state.get_window_anchor(str(integration.id), key) == "2026-08-08"
    assert await handlers.gnw_state.is_quiet_period(str(integration.id), key)


@pytest.mark.asyncio
async def test_run_query_job_sync_no_anchor_advance_when_noncontiguous(mocker, handler_env):
    handlers, send, state_stub, integration = handler_env
    mocker.patch.object(DataAPI, "query_sync", AsyncMock(return_value=[]))
    from app.actions.configurations import entry_state_key
    entry = DatasetEntry(dataset="nasa_viirs_fire_alerts")
    key = entry_state_key(entry)
    await handlers.gnw_state.set_window_anchor(str(integration.id), key, "2026-08-01")

    config = RunQueryJobConfig(entry=entry, geostore_ids=["geo-1"],
                               window_start=datetime.date(2026, 8, 8),  # not == anchor
                               window_end=datetime.date(2026, 8, 15))
    await handlers.action_run_query_job(integration, config)
    assert await handlers.gnw_state.get_window_anchor(str(integration.id), key) == "2026-08-01"


@pytest.mark.asyncio
async def test_run_query_job_batch_submits_and_persists(mocker, handler_env):
    handlers, send, state_stub, integration = handler_env
    mocker.patch.object(DataAPI, "query_batch", AsyncMock(return_value=JobResponse.Data(
        job_id="j1", job_link="https://api.example.com/job/j1", status="pending")))
    from app.actions.configurations import entry_state_key
    entry = DatasetEntry(dataset="gfw_integrated_alerts")
    key = entry_state_key(entry)

    config = RunQueryJobConfig(entry=entry, geostore_ids=["geo-1"],
                               window_start=datetime.date(2026, 7, 9),
                               window_end=datetime.date(2026, 8, 8), submit_new=True)
    result = await handlers.action_run_query_job(integration, config)

    assert result["jobs_submitted"] == 1
    jobs = await handlers.gnw_state.get_pending_jobs(str(integration.id), key)
    assert jobs[0]["job_id"] == "j1"
    assert jobs[0]["window_end"] == "2026-08-08"
    # A submitted job sets the long (240-720 min) quiet tier.
    assert await handlers.gnw_state.is_quiet_period(str(integration.id), key)


@pytest.mark.asyncio
async def test_run_query_job_batch_collects_completed_job(mocker, handler_env):
    handlers, send, state_stub, integration = handler_env
    from app.actions.configurations import entry_state_key
    entry = DatasetEntry(dataset="gfw_integrated_alerts")
    key = entry_state_key(entry)
    await handlers.gnw_state.set_window_anchor(str(integration.id), key, "2026-07-09")
    await handlers.gnw_state.add_pending_job(str(integration.id), key, {
        "job_id": "j1", "job_link": "https://api/job/j1",
        "window_start": "2026-07-09", "window_end": "2026-08-08"})
    mocker.patch.object(DataAPI, "get_job_status", AsyncMock(return_value=JobResponse.Data(
        job_id="j1", status="success", download_link="https://dl.example.com/x")))
    mocker.patch.object(DataAPI, "download_job_results", AsyncMock(return_value=[
        {"latitude": 1.0, "longitude": 2.0, "gfw_integrated_alerts__date": "2026-08-01",
         "gfw_integrated_alerts__confidence": "high"}]))

    config = RunQueryJobConfig(entry=entry, geostore_ids=["geo-1"],
                               window_start=datetime.date(2026, 7, 9),
                               window_end=datetime.date(2026, 8, 8), submit_new=False)
    result = await handlers.action_run_query_job(integration, config)

    assert result["jobs_completed"] == 1 and result["events_posted"] == 1
    assert await handlers.gnw_state.get_pending_jobs(str(integration.id), key) == []
    assert await handlers.gnw_state.get_window_anchor(str(integration.id), key) == "2026-08-08"


@pytest.mark.asyncio
async def test_run_query_job_batch_failed_job_marks_failed_and_clears_pending(mocker, handler_env):
    handlers, send, state_stub, integration = handler_env
    from app.actions.configurations import entry_state_key
    entry = DatasetEntry(dataset="gfw_integrated_alerts")
    key = entry_state_key(entry)
    await handlers.gnw_state.set_window_anchor(str(integration.id), key, "2026-07-09")
    await handlers.gnw_state.add_pending_job(str(integration.id), key, {
        "job_id": "j1", "job_link": "https://api.example.com/job/j1",
        "window_start": "2026-07-09", "window_end": "2026-08-08"})
    mocker.patch.object(DataAPI, "get_job_status", AsyncMock(return_value=JobResponse.Data(
        job_id="j1", status="failed", message="boom")))

    config = RunQueryJobConfig(entry=entry, geostore_ids=["geo-1"],
                               window_start=datetime.date(2026, 7, 9),
                               window_end=datetime.date(2026, 8, 8), submit_new=False)
    result = await handlers.action_run_query_job(integration, config)

    assert result["jobs_failed"] == 1
    assert await handlers.gnw_state.get_pending_jobs(str(integration.id), key) == []
    # A failed job must not advance the anchor: the window is re-covered next run.
    assert await handlers.gnw_state.get_window_anchor(str(integration.id), key) == "2026-07-09"


@pytest.mark.asyncio
async def test_run_query_job_batch_expired_download_link_marks_failed(mocker, handler_env):
    handlers, send, state_stub, integration = handler_env
    from app.actions.configurations import entry_state_key
    entry = DatasetEntry(dataset="gfw_integrated_alerts")
    key = entry_state_key(entry)
    await handlers.gnw_state.set_window_anchor(str(integration.id), key, "2026-07-09")
    await handlers.gnw_state.add_pending_job(str(integration.id), key, {
        "job_id": "j1", "job_link": "https://api.example.com/job/j1",
        "window_start": "2026-07-09", "window_end": "2026-08-08"})
    mocker.patch.object(DataAPI, "get_job_status", AsyncMock(return_value=JobResponse.Data(
        job_id="j1", status="success", download_link="https://dl.example.com/x")))
    mocker.patch.object(DataAPI, "download_job_results", AsyncMock(
        side_effect=DownloadLinkExpiredException("link expired")))

    config = RunQueryJobConfig(entry=entry, geostore_ids=["geo-1"],
                               window_start=datetime.date(2026, 7, 9),
                               window_end=datetime.date(2026, 8, 8), submit_new=False)
    result = await handlers.action_run_query_job(integration, config)

    assert result["jobs_failed"] == 1
    assert await handlers.gnw_state.get_pending_jobs(str(integration.id), key) == []
    assert await handlers.gnw_state.get_window_anchor(str(integration.id), key) == "2026-07-09"


@pytest.mark.asyncio
async def test_run_query_job_batch_post_failure_keeps_job_pending_for_retry(mocker, handler_env):
    handlers, send, state_stub, integration = handler_env
    from app.actions.configurations import entry_state_key
    entry = DatasetEntry(dataset="gfw_integrated_alerts")
    key = entry_state_key(entry)
    await handlers.gnw_state.set_window_anchor(str(integration.id), key, "2026-07-09")
    await handlers.gnw_state.add_pending_job(str(integration.id), key, {
        "job_id": "j1", "job_link": "https://api.example.com/job/j1",
        "window_start": "2026-07-09", "window_end": "2026-08-08"})
    mocker.patch.object(DataAPI, "get_job_status", AsyncMock(return_value=JobResponse.Data(
        job_id="j1", status="success", download_link="https://dl.example.com/x")))
    mocker.patch.object(DataAPI, "download_job_results", AsyncMock(return_value=[
        {"latitude": 1.0, "longitude": 2.0, "gfw_integrated_alerts__date": "2026-08-01",
         "gfw_integrated_alerts__confidence": "high"}]))
    # Simulate the post to Gundi failing after a successful download.
    send.side_effect = Exception("gundi is down")

    config = RunQueryJobConfig(entry=entry, geostore_ids=["geo-1"],
                               window_start=datetime.date(2026, 7, 9),
                               window_end=datetime.date(2026, 8, 8), submit_new=False)
    result = await handlers.action_run_query_job(integration, config)

    assert result["jobs_pending"] == 1
    assert result["jobs_completed"] == 0
    # Job must NOT be removed: the next run must retry the collect+post (at-least-once).
    jobs = await handlers.gnw_state.get_pending_jobs(str(integration.id), key)
    assert len(jobs) == 1 and jobs[0]["job_id"] == "j1"
    assert await handlers.gnw_state.get_window_anchor(str(integration.id), key) == "2026-07-09"


@pytest.mark.asyncio
async def test_run_query_job_batch_still_pending_sets_zero_minute_quiet_tier(mocker, handler_env):
    handlers, send, state_stub, integration = handler_env
    from app.actions.configurations import entry_state_key
    entry = DatasetEntry(dataset="gfw_integrated_alerts")
    key = entry_state_key(entry)
    await handlers.gnw_state.add_pending_job(str(integration.id), key, {
        "job_id": "j1", "job_link": "https://api.example.com/job/j1",
        "window_start": "2026-07-09", "window_end": "2026-08-08"})
    mocker.patch.object(DataAPI, "get_job_status", AsyncMock(return_value=JobResponse.Data(
        job_id="j1", status="pending")))

    config = RunQueryJobConfig(entry=entry, geostore_ids=["geo-1"],
                               window_start=datetime.date(2026, 7, 9),
                               window_end=datetime.date(2026, 8, 8), submit_new=False)
    result = await handlers.action_run_query_job(integration, config)

    assert result["jobs_pending"] == 1
    # jobs_pending > 0 -> 0-minute quiet tier, i.e. set_quiet_period is a no-op.
    assert not await handlers.gnw_state.is_quiet_period(str(integration.id), key)
