import datetime
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.actions.jobs import BatchQueryJob, JobState, SyncQueryJob, generate_windows


def test_generate_windows_contiguous_oldest_first():
    windows = generate_windows(datetime.date(2026, 8, 1), datetime.date(2026, 8, 18), interval_days=7)
    assert windows == [
        (datetime.date(2026, 8, 1), datetime.date(2026, 8, 8)),
        (datetime.date(2026, 8, 8), datetime.date(2026, 8, 15)),
        (datetime.date(2026, 8, 15), datetime.date(2026, 8, 18)),
    ]


def test_generate_windows_short_range_single_window():
    assert generate_windows(datetime.date(2026, 8, 1), datetime.date(2026, 8, 3)) == [
        (datetime.date(2026, 8, 1), datetime.date(2026, 8, 3))
    ]


@pytest.mark.asyncio
async def test_sync_job_runs_inline():
    client = MagicMock()
    client.query_sync = AsyncMock(return_value=[{"latitude": 1.0}])
    job = SyncQueryJob(client, dataset="d", sql="SELECT ...", geostore_id="g1")
    assert await job.start() == JobState.RESULTS_READY
    assert job.collect() == [{"latitude": 1.0}]
    client.query_sync.assert_awaited_once_with(dataset="d", sql="SELECT ...", geostore_id="g1")


@pytest.mark.asyncio
async def test_batch_job_submits_and_polls():
    from app.actions.gnwclient import JobResponse
    client = MagicMock()
    client.query_batch = AsyncMock(return_value=JobResponse.Data(
        job_id="j1", job_link="https://api.example.com/job/j1", status="pending"))
    job = BatchQueryJob(client, dataset="d", sql="SELECT ...", geostore_ids=["g1"])
    assert await job.start() == JobState.PENDING
    assert job.job_record["job_id"] == "j1"
    assert job.job_record["job_link"] == "https://api.example.com/job/j1"

    client.get_job_status = AsyncMock(return_value=JobResponse.Data(
        job_id="j1", status="success", download_link="https://dl.example.com/x"))
    state, download_link, message = await job.check("https://api.example.com/job/j1")
    assert state == JobState.RESULTS_READY and download_link == "https://dl.example.com/x"

    client.get_job_status = AsyncMock(return_value=JobResponse.Data(
        job_id="j1", status="partial_success", download_link="https://dl.example.com/x"))
    state, download_link, message = await job.check("https://api.example.com/job/j1")
    assert state == JobState.RESULTS_READY and message == "partial_success"

    client.get_job_status = AsyncMock(return_value=JobResponse.Data(
        job_id="j1", status="failed", message="boom"))
    state, download_link, message = await job.check("https://api.example.com/job/j1")
    assert state == JobState.FAILED and message == "boom"

    client.get_job_status = AsyncMock(return_value=JobResponse.Data(
        job_id="j1", status="success", download_link=None))
    state, _, _ = await job.check("https://api.example.com/job/j1")
    assert state == JobState.PENDING  # success without link is still pending

    client.download_job_results = AsyncMock(return_value=[{"latitude": 2.0}])
    assert await job.collect("https://dl.example.com/x") == [{"latitude": 2.0}]
