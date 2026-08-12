"""QueryJob seam: sync and batch dataset fetches behind one lifecycle.

SyncQueryJob completes inline and holds no state; BatchQueryJob's pending
record is persisted by the caller (see GnwState.add_pending_job).
"""
from datetime import date, timedelta
from enum import Enum
from typing import List, Optional, Tuple

from app.actions.gnwclient import DataAPI


class JobState(str, Enum):
    PENDING = "pending"
    RESULTS_READY = "results_ready"
    FAILED = "failed"
    EXPIRED = "expired"


def generate_windows(start: date, end: date, interval_days: int = 7) -> List[Tuple[date, date]]:
    """Half-open [start, end) slices covering [start, end], oldest first.
    The upper bound of one window equals the lower bound of the next; the SQL
    interval is half-open, so shared edges do not double-count."""
    windows = []
    lower = start
    while lower < end:
        upper = min(end, lower + timedelta(days=interval_days))
        windows.append((lower, upper))
        lower = upper
    return windows


class SyncQueryJob:
    def __init__(self, client: DataAPI, *, dataset: str, sql: str, geostore_id: str):
        self._client = client
        self._dataset, self._sql, self._geostore_id = dataset, sql, geostore_id
        self._rows: List[dict] = []

    async def start(self) -> JobState:
        self._rows = await self._client.query_sync(
            dataset=self._dataset, sql=self._sql, geostore_id=self._geostore_id
        ) or []
        return JobState.RESULTS_READY

    def collect(self) -> List[dict]:
        return self._rows


class BatchQueryJob:
    def __init__(self, client: DataAPI, *, dataset: str, sql: str, geostore_ids: List[str]):
        self._client = client
        self._dataset, self._sql, self._geostore_ids = dataset, sql, geostore_ids
        self.job_record: dict = {}

    async def start(self) -> JobState:
        job = await self._client.query_batch(
            dataset=self._dataset, sql=self._sql, geostore_ids=self._geostore_ids
        )
        self.job_record = {"job_id": job.job_id, "job_link": str(job.job_link)}
        return JobState.PENDING

    async def check(self, job_link: str) -> Tuple[JobState, Optional[str], Optional[str]]:
        status = await self._client.get_job_status(job_link)
        if status.status in ("success", "partial_success") and status.download_link:
            return JobState.RESULTS_READY, str(status.download_link), (
                "partial_success" if status.status == "partial_success" else None
            )
        if status.status == "failed":
            return JobState.FAILED, None, status.message
        return JobState.PENDING, None, None

    async def collect(self, download_link: str) -> List[dict]:
        return await self._client.download_job_results(download_link)
