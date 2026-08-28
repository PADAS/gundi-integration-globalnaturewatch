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
async def test_post_rows_dedups_repeat_posts(mocker, handler_env):
    """The same row posted twice through _post_rows must only reach Gundi once."""
    handlers, send, state_stub, integration = handler_env
    from app.actions.configurations import DatasetEntry
    from app.actions.datasets import DATASET_REGISTRY
    entry = DatasetEntry(dataset="nasa_viirs_fire_alerts")
    spec = DATASET_REGISTRY["nasa_viirs_fire_alerts"]
    rows = [{"latitude": 1.0, "longitude": 2.0, "alert__date": "2026-08-05",
             "confidence__cat": "h", "frp__MW": 3.0}]

    first = await handlers._post_rows(rows, entry, spec, str(integration.id))
    assert first == 1
    assert send.await_count == 1

    send.reset_mock()
    second = await handlers._post_rows(rows, entry, spec, str(integration.id))
    assert second == 0
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_rows_failed_send_leaves_rows_retryable(mocker, handler_env):
    handlers, send, state_stub, integration = handler_env
    from app.actions.configurations import DatasetEntry
    from app.actions.datasets import DATASET_REGISTRY
    entry = DatasetEntry(dataset="nasa_viirs_fire_alerts")
    spec = DATASET_REGISTRY["nasa_viirs_fire_alerts"]
    rows = [{"latitude": 1.0, "longitude": 2.0, "alert__date": "2026-08-05",
             "confidence__cat": "h", "frp__MW": 3.0}]

    send.side_effect = Exception("gundi is down")
    with pytest.raises(Exception, match="gundi is down"):
        await handlers._post_rows(rows, entry, spec, str(integration.id))

    send.side_effect = None
    send.reset_mock()
    result = await handlers._post_rows(rows, entry, spec, str(integration.id))
    assert result == 1
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_post_rows_h3_counts_new_rows_only(mocker, handler_env):
    handlers, send, state_stub, integration = handler_env
    from app.actions.configurations import DatasetEntry, H3GridOutput
    from app.actions.datasets import DATASET_REGISTRY
    entry = DatasetEntry(dataset="nasa_viirs_fire_alerts", output=H3GridOutput(resolution=7))
    spec = DATASET_REGISTRY["nasa_viirs_fire_alerts"]
    row_a = {"latitude": 1.0, "longitude": 2.0, "alert__date": "2026-08-05",
             "confidence__cat": "h", "frp__MW": 3.0}
    row_b = {"latitude": 1.0001, "longitude": 2.0001, "alert__date": "2026-08-05",
             "confidence__cat": "h", "frp__MW": 4.0}
    row_c = {"latitude": 1.0002, "longitude": 2.0002, "alert__date": "2026-08-06",
             "confidence__cat": "n", "frp__MW": 5.0}

    first = await handlers._post_rows([row_a, row_b], entry, spec, str(integration.id))
    assert first == 1
    posted = send.call_args.kwargs["events"]
    assert posted[0]["event_details"]["record_count"] == 2

    send.reset_mock()
    second = await handlers._post_rows([row_a, row_b, row_c], entry, spec, str(integration.id))
    assert second == 1
    posted = send.call_args.kwargs["events"]
    assert posted[0]["event_details"]["record_count"] == 1


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
async def test_run_query_job_batch_failed_job_logs_reason(mocker, caplog, handler_env):
    """The failure reason must reach BOTH the activity-log title and the GCP
    (Cloud Run) logs — previously neither carried it, so a failed job left no
    clues anywhere."""
    import logging
    handlers, send, state_stub, integration = handler_env
    from app.actions.configurations import entry_state_key
    entry = DatasetEntry(dataset="gfw_integrated_alerts")
    key = entry_state_key(entry)
    await handlers.gnw_state.add_pending_job(str(integration.id), key, {
        "job_id": "j1", "job_link": "https://api.example.com/job/j1",
        "window_start": "2026-07-09", "window_end": "2026-08-08"})
    mocker.patch.object(DataAPI, "get_job_status", AsyncMock(return_value=JobResponse.Data(
        job_id="j1", status="failed", message="Unsupported filter operator: in")))

    config = RunQueryJobConfig(entry=entry, geostore_ids=["geo-1"],
                               window_start=datetime.date(2026, 7, 9),
                               window_end=datetime.date(2026, 8, 8), submit_new=False)
    with caplog.at_level(logging.ERROR, logger="app.actions.handlers"):
        await handlers.action_run_query_job(integration, config)

    titles = [c.kwargs["title"] for c in handlers.log_action_activity.await_args_list
              if c.kwargs.get("title", "").startswith("Batch job")]
    assert titles == ["Batch job j1 failed: Unsupported filter operator: in"]
    assert any("j1" in r.message and "Unsupported filter operator: in" in r.message
               for r in caplog.records)


@pytest.mark.asyncio
async def test_run_query_job_batch_expired_download_link_stays_pending(mocker, handler_env):
    """An expired download link is transient: the next poll returns a freshly
    signed link, so the job must remain pending (bounded by the record TTL),
    not be dropped as failed."""
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

    assert result["jobs_pending"] == 1
    assert result["jobs_failed"] == 0
    pending = await handlers.gnw_state.get_pending_jobs(str(integration.id), key)
    assert [j["job_id"] for j in pending] == ["j1"]
    assert await handlers.gnw_state.get_window_anchor(str(integration.id), key) == "2026-07-09"
    send.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_run_query_job_sync_logs_query_result(mocker, handler_env):
    handlers, send, state_stub, integration = handler_env
    mocker.patch.object(DataAPI, "query_sync", AsyncMock(return_value=[
        {"latitude": 1.0, "longitude": 2.0, "alert__date": "2026-08-05",
         "confidence__cat": "h", "frp__MW": 3.0},
    ]))
    config = RunQueryJobConfig(entry=DatasetEntry(dataset="nasa_viirs_fire_alerts"),
                               geostore_ids=["geo-1"],
                               window_start=datetime.date(2026, 8, 1),
                               window_end=datetime.date(2026, 8, 8))
    await handlers.action_run_query_job(integration, config)

    titles = [c.kwargs["title"] for c in handlers.log_action_activity.await_args_list
              if c.kwargs.get("title", "").startswith("Queried")]
    assert titles == [
        "Queried nasa_viirs_fire_alerts 2026-08-01→2026-08-08: 1 rows fetched, 1 events posted"]


@pytest.mark.asyncio
async def test_run_query_job_batch_logs_job_counts(mocker, handler_env):
    handlers, send, state_stub, integration = handler_env
    mocker.patch.object(DataAPI, "query_batch", AsyncMock(return_value=JobResponse.Data(
        job_id="j1", job_link="https://api.example.com/job/j1", status="pending")))
    config = RunQueryJobConfig(entry=DatasetEntry(dataset="gfw_integrated_alerts"),
                               geostore_ids=["geo-1"],
                               window_start=datetime.date(2026, 7, 9),
                               window_end=datetime.date(2026, 8, 8), submit_new=True)
    await handlers.action_run_query_job(integration, config)

    titles = [c.kwargs["title"] for c in handlers.log_action_activity.await_args_list
              if c.kwargs.get("title", "").startswith("Queried")]
    assert titles == [
        "Queried gfw_integrated_alerts 2026-07-09→2026-08-08: 0 rows fetched, 0 events posted;"
        " jobs: 1 submitted, 0 completed, 0 pending, 0 failed"]


def make_aoi_dict(geostore="geo-1"):
    return {"type": "area", "id": "aoi-1", "attributes": {
        "name": "A", "application": "gfw", "geostore": geostore,
        "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z",
        "use": {}, "env": "production", "tags": [], "status": "saved", "public": True}}


@pytest.fixture
def orchestrator_env(mocker, handler_env):
    handlers, send, state_stub, integration = handler_env
    # activity_logger() publishes IntegrationActionStarted/Complete/Failed events over
    # real GCP PubSub; stub it out so the orchestrator tests stay hermetic and fast.
    mocker.patch("app.services.activity_logger.publish_event", AsyncMock())
    trigger = mocker.patch.object(handlers, "trigger_action", AsyncMock())
    mocker.patch.object(DataAPI, "get_dataset_metadata", AsyncMock(
        return_value=__import__("app.actions.gnwclient", fromlist=["DatasetResponseItem"]).DatasetResponseItem(
            created_on="2026-08-01T00:00:00Z", updated_on="2026-08-09T00:00:00Z",
            dataset="nasa_viirs_fire_alerts", version="v20260809", is_latest=True, is_mutable=True)))
    return handlers, trigger, state_stub, integration


@pytest.mark.asyncio
async def test_pull_events_uses_cached_aoi_and_triggers_sync_windows(mocker, orchestrator_env):
    handlers, trigger, state_stub, integration = orchestrator_env
    await handlers.gnw_state.set_aoi_data(str(integration.id), make_aoi_dict())
    from app.actions.configurations import PullEventsConfig
    config = PullEventsConfig.parse_obj({
        "aoi_url": "https://www.globalnaturewatch.org/dashboards/aoi/abc/",
        "dataset_entries": [{"dataset": "nasa_viirs_fire_alerts", "lookback_days": 10}]})
    result = await handlers.action_pull_events(integration, config)
    assert result["errors"] == []
    assert trigger.await_count == 2  # 10-day lookback -> two 7-day windows
    called_action = trigger.call_args.args[1]
    assert called_action == "run_query_job"


@pytest.mark.asyncio
async def test_pull_events_steady_state_recovers_late_ingested_data(mocker, orchestrator_env):
    """Day-N run with a day-N anchor must still re-query the reingest margin."""
    handlers, trigger, state_stub, integration = orchestrator_env
    await handlers.gnw_state.set_aoi_data(str(integration.id), make_aoi_dict())
    from app.actions.configurations import DatasetEntry, PullEventsConfig, entry_state_key
    from datetime import date, timedelta
    entry = DatasetEntry(dataset="nasa_viirs_fire_alerts")
    key = entry_state_key(entry)
    tomorrow = handlers._next_midnight_utc().date()
    # anchor is already at "tomorrow" (yesterday's run anchored to its window end)
    await handlers.gnw_state.set_window_anchor(str(integration.id), key, tomorrow.isoformat())
    config = PullEventsConfig.parse_obj({
        "aoi_url": "https://www.globalnaturewatch.org/dashboards/aoi/abc/",
        "dataset_entries": [{"dataset": "nasa_viirs_fire_alerts"}]})
    result = await handlers.action_pull_events(integration, config)
    # margin=2 for viirs: the run must trigger a [tomorrow-2, tomorrow) window, not skip
    assert trigger.await_count == 1
    sub_config = trigger.call_args.kwargs["config"]
    assert sub_config.window_start == tomorrow - timedelta(days=2)
    assert sub_config.window_end == tomorrow
    # and the anchor was pulled back to the effective start before fan-out
    assert await handlers.gnw_state.get_window_anchor(str(integration.id), key) == (tomorrow - timedelta(days=2)).isoformat()


@pytest.mark.asyncio
async def test_pull_events_stuck_anchor_recovers_to_lookback_start(mocker, orchestrator_env):
    """A stored anchor far behind the lookback window must not stay stuck: the
    orchestrator clamps it up to lookback_start and persists that BEFORE fan-out,
    so the contiguous-advance rule in run_query_job can fire on subsequent runs."""
    handlers, trigger, state_stub, integration = orchestrator_env
    await handlers.gnw_state.set_aoi_data(str(integration.id), make_aoi_dict())
    from app.actions.configurations import DatasetEntry, PullEventsConfig, entry_state_key
    from app.actions.datasets import DATASET_REGISTRY
    entry = DatasetEntry(dataset="nasa_viirs_fire_alerts")
    key = entry_state_key(entry)
    await handlers.gnw_state.set_window_anchor(str(integration.id), key, "2020-01-01")
    end = handlers._next_midnight_utc().date()
    spec = DATASET_REGISTRY["nasa_viirs_fire_alerts"]
    lookback_start = end - datetime.timedelta(days=entry.resolved_lookback_days(spec))
    config = PullEventsConfig.parse_obj({
        "aoi_url": "https://www.globalnaturewatch.org/dashboards/aoi/abc/",
        "dataset_entries": [{"dataset": "nasa_viirs_fire_alerts"}]})
    result = await handlers.action_pull_events(integration, config)
    assert await handlers.gnw_state.get_window_anchor(str(integration.id), key) == lookback_start.isoformat()
    # all triggered windows fall within [lookback_start, end)
    for call in trigger.call_args_list:
        sub_config = call.kwargs["config"]
        assert sub_config.window_start >= lookback_start
        assert sub_config.window_end <= end


@pytest.mark.asyncio
async def test_pull_events_skips_on_quiet_period(mocker, orchestrator_env):
    handlers, trigger, state_stub, integration = orchestrator_env
    await handlers.gnw_state.set_aoi_data(str(integration.id), make_aoi_dict())
    from app.actions.configurations import DatasetEntry, PullEventsConfig, entry_state_key
    from datetime import timedelta
    entry = DatasetEntry(dataset="nasa_viirs_fire_alerts")
    await handlers.gnw_state.set_quiet_period(str(integration.id), entry_state_key(entry), timedelta(minutes=30))
    config = PullEventsConfig.parse_obj({
        "aoi_url": "https://www.globalnaturewatch.org/dashboards/aoi/abc/",
        "dataset_entries": [{"dataset": "nasa_viirs_fire_alerts"}]})
    result = await handlers.action_pull_events(integration, config)
    assert trigger.await_count == 0
    assert len(result["skipped"]) == 1


@pytest.mark.asyncio
async def test_pull_events_version_gate_skips(mocker, orchestrator_env):
    handlers, trigger, state_stub, integration = orchestrator_env
    await handlers.gnw_state.set_aoi_data(str(integration.id), make_aoi_dict())
    state_stub.get_state = AsyncMock(return_value={
        "dataset": "nasa_viirs_fire_alerts", "version": "v20260809",
        "latest_updated_on": "2026-08-09T00:00:00+00:00"})
    from app.actions.configurations import PullEventsConfig
    config = PullEventsConfig.parse_obj({
        "aoi_url": "https://www.globalnaturewatch.org/dashboards/aoi/abc/",
        "dataset_entries": [{"dataset": "nasa_viirs_fire_alerts"}]})
    result = await handlers.action_pull_events(integration, config)
    assert trigger.await_count == 0


@pytest.mark.asyncio
async def test_pull_events_bad_entry_isolated(mocker, orchestrator_env):
    handlers, trigger, state_stub, integration = orchestrator_env
    await handlers.gnw_state.set_aoi_data(str(integration.id), make_aoi_dict())
    from app.actions.configurations import PullEventsConfig
    config = PullEventsConfig.parse_obj({
        "aoi_url": "https://www.globalnaturewatch.org/dashboards/aoi/abc/",
        "dataset_entries": [
            {"dataset": "not_in_registry"},
            {"dataset": "nasa_viirs_fire_alerts", "filters": [
                {"field": "no_such_field", "operator": "=", "value": "x"}]},
            {"dataset": "nasa_viirs_fire_alerts"},
        ]})
    result = await handlers.action_pull_events(integration, config)
    assert len(result["errors"]) == 2       # first two entries
    assert trigger.await_count >= 1          # third entry still ran


@pytest.mark.asyncio
async def test_pull_events_batch_pending_jobs_bypass_quiet(mocker, orchestrator_env):
    handlers, trigger, state_stub, integration = orchestrator_env
    await handlers.gnw_state.set_aoi_data(str(integration.id), make_aoi_dict())
    from app.actions.configurations import DatasetEntry, PullEventsConfig, entry_state_key
    from datetime import timedelta
    entry = DatasetEntry(dataset="gfw_integrated_alerts")
    key = entry_state_key(entry)
    await handlers.gnw_state.set_quiet_period(str(integration.id), key, timedelta(minutes=60))
    await handlers.gnw_state.add_pending_job(str(integration.id), key,
                                             {"job_id": "j1", "job_link": "https://x/j1"})
    config = PullEventsConfig.parse_obj({
        "aoi_url": "https://www.globalnaturewatch.org/dashboards/aoi/abc/",
        "dataset_entries": [{"dataset": "gfw_integrated_alerts"}]})
    await handlers.action_pull_events(integration, config)
    assert trigger.await_count == 1
    sub_config = trigger.call_args.kwargs["config"]
    assert sub_config.submit_new is False


def make_pull_config_mock(entries):
    pull_cfg = MagicMock()
    pull_cfg.action.value = "pull_events"
    pull_cfg.data = {
        "aoi_url": "https://www.globalnaturewatch.org/dashboards/aoi/abc/",
        "dataset_entries": entries,
    }
    return pull_cfg


@pytest.mark.asyncio
async def test_reset_quiet_periods_clears_all_entries(handler_env):
    handlers, send, state_stub, integration = handler_env
    from app.actions.configurations import DatasetEntry, ResetQuietPeriodsConfig, entry_state_key
    from datetime import timedelta
    integration.configurations.append(make_pull_config_mock([
        {"dataset": "nasa_viirs_fire_alerts"},
        {"dataset": "gfw_integrated_alerts"},
    ]))
    integration_id = str(integration.id)
    viirs_key = entry_state_key(DatasetEntry(dataset="nasa_viirs_fire_alerts"))
    gfw_key = entry_state_key(DatasetEntry(dataset="gfw_integrated_alerts"))
    await handlers.gnw_state.set_quiet_period(integration_id, viirs_key, timedelta(minutes=60))
    await handlers.gnw_state.set_quiet_period(integration_id, gfw_key, timedelta(minutes=60))

    result = await handlers.action_reset_quiet_periods(integration, ResetQuietPeriodsConfig())

    assert not await handlers.gnw_state.is_quiet_period(integration_id, viirs_key)
    assert not await handlers.gnw_state.is_quiet_period(integration_id, gfw_key)
    # version-gate status cleared too, or the next run would re-skip on "no dataset updates"
    cleared = {c.kwargs.get("source_id") or c.args[2] for c in state_stub.delete_state.await_args_list}
    assert cleared == {"nasa_viirs_fire_alerts", "gfw_integrated_alerts"}
    assert result["reset"] == ["nasa_viirs_fire_alerts", "gfw_integrated_alerts"]


@pytest.mark.asyncio
async def test_reset_quiet_periods_dataset_filter_only_clears_matching(handler_env):
    handlers, send, state_stub, integration = handler_env
    from app.actions.configurations import DatasetEntry, ResetQuietPeriodsConfig, entry_state_key
    from datetime import timedelta
    integration.configurations.append(make_pull_config_mock([
        {"dataset": "nasa_viirs_fire_alerts"},
        {"dataset": "gfw_integrated_alerts"},
    ]))
    integration_id = str(integration.id)
    viirs_key = entry_state_key(DatasetEntry(dataset="nasa_viirs_fire_alerts"))
    gfw_key = entry_state_key(DatasetEntry(dataset="gfw_integrated_alerts"))
    await handlers.gnw_state.set_quiet_period(integration_id, viirs_key, timedelta(minutes=60))
    await handlers.gnw_state.set_quiet_period(integration_id, gfw_key, timedelta(minutes=60))

    result = await handlers.action_reset_quiet_periods(
        integration, ResetQuietPeriodsConfig(dataset="nasa_viirs_fire_alerts"))

    assert not await handlers.gnw_state.is_quiet_period(integration_id, viirs_key)
    assert await handlers.gnw_state.is_quiet_period(integration_id, gfw_key)
    assert result["reset"] == ["nasa_viirs_fire_alerts"]


@pytest.mark.asyncio
async def test_reset_quiet_periods_without_pull_config_reports_message(handler_env):
    handlers, send, state_stub, integration = handler_env
    from app.actions.configurations import ResetQuietPeriodsConfig

    result = await handlers.action_reset_quiet_periods(integration, ResetQuietPeriodsConfig())

    assert result["reset"] == []
    assert "pull" in result["message"].lower()


@pytest.mark.asyncio
async def test_reset_quiet_periods_dataset_not_in_config_reports_message(handler_env):
    handlers, send, state_stub, integration = handler_env
    from app.actions.configurations import ResetQuietPeriodsConfig
    integration.configurations.append(make_pull_config_mock([
        {"dataset": "nasa_viirs_fire_alerts"},
    ]))

    result = await handlers.action_reset_quiet_periods(
        integration, ResetQuietPeriodsConfig(dataset="gfw_integrated_alerts"))

    assert result["reset"] == []
    assert "gfw_integrated_alerts" in result["message"]


def summary_titles(log_mock, prefix):
    return [c.kwargs["title"] for c in log_mock.await_args_list
            if c.kwargs.get("title", "").startswith(prefix)]


@pytest.mark.asyncio
async def test_pull_events_logs_summary_with_queried_datasets(mocker, orchestrator_env):
    handlers, trigger, state_stub, integration = orchestrator_env
    await handlers.gnw_state.set_aoi_data(str(integration.id), make_aoi_dict())
    from app.actions.configurations import PullEventsConfig
    config = PullEventsConfig.parse_obj({
        "aoi_url": "https://www.globalnaturewatch.org/dashboards/aoi/abc/",
        "dataset_entries": [{"dataset": "nasa_viirs_fire_alerts"}]})
    await handlers.action_pull_events(integration, config)

    titles = summary_titles(handlers.log_action_activity, "Pull events")
    assert len(titles) == 1
    assert "queried: entry[0]:nasa_viirs_fire_alerts" in titles[0]


@pytest.mark.asyncio
async def test_pull_events_logs_summary_with_skip_reason(mocker, orchestrator_env):
    handlers, trigger, state_stub, integration = orchestrator_env
    await handlers.gnw_state.set_aoi_data(str(integration.id), make_aoi_dict())
    from app.actions.configurations import DatasetEntry, PullEventsConfig, entry_state_key
    from datetime import timedelta
    entry = DatasetEntry(dataset="nasa_viirs_fire_alerts")
    await handlers.gnw_state.set_quiet_period(str(integration.id), entry_state_key(entry), timedelta(minutes=30))
    config = PullEventsConfig.parse_obj({
        "aoi_url": "https://www.globalnaturewatch.org/dashboards/aoi/abc/",
        "dataset_entries": [{"dataset": "nasa_viirs_fire_alerts"}]})
    await handlers.action_pull_events(integration, config)

    titles = summary_titles(handlers.log_action_activity, "Pull events")
    assert len(titles) == 1
    assert "skipped: entry[0]:nasa_viirs_fire_alerts (quiet period)" in titles[0]


@pytest.mark.asyncio
async def test_action_auth_validates_credentials(mocker, integration_v2_like):
    import app.actions.handlers as handlers
    from app.actions.configurations import AuthenticateConfig
    import pydantic
    mocker.patch.object(DataAPI, "get_access_token", AsyncMock(return_value=MagicMock()))
    config = AuthenticateConfig(email="u@example.com", password=pydantic.SecretStr("pw"))
    result = await handlers.action_auth(integration_v2_like, config)
    assert result == {"valid_credentials": True}
