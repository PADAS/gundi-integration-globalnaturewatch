import json
import time
from datetime import timedelta
from typing import List, Optional, Set


class GnwState:
    """Integration-owned state on top of the template's IntegrationStateManager.

    Lives in app/actions/ (NOT app/services/state.py) so template syncs stay
    conflict-free. Keys are namespaced under 'gnw.'.
    """

    AOI_TTL_SECONDS = 86400 * 7
    JOB_TTL_SECONDS = 86400

    def __init__(self, state_manager):
        self._manager = state_manager
        self._db = state_manager.db_client

    # --- AOI cache ---
    def _aoi_key(self, integration_id: str) -> str:
        return f"gnw.{integration_id}.aoi_data"

    async def set_aoi_data(self, integration_id: str, aoi_data: dict):
        await self._db.setex(self._aoi_key(integration_id), self.AOI_TTL_SECONDS,
                             json.dumps(aoi_data, default=str))

    async def get_aoi_data(self, integration_id: str) -> Optional[dict]:
        data = await self._db.get(self._aoi_key(integration_id))
        return json.loads(data) if data else None

    # --- quiet periods (per entry) ---
    def _quiet_key(self, integration_id: str, entry_key: str) -> str:
        return f"gnw.{integration_id}.{entry_key}.quiet_period"

    async def is_quiet_period(self, integration_id: str, entry_key: str) -> bool:
        return bool(await self._db.exists(self._quiet_key(integration_id, entry_key)))

    async def set_quiet_period(self, integration_id: str, entry_key: str, period: timedelta):
        seconds = int(period.total_seconds())
        if seconds <= 0:
            return
        await self._db.setex(self._quiet_key(integration_id, entry_key), seconds, "1")

    # --- pending batch jobs (per entry) ---
    def _pending_job_key(self, integration_id: str, entry_key: str, job_id: str) -> str:
        return f"gnw.{integration_id}.{entry_key}.pending_job.{job_id}"

    def _pending_jobs_set_key(self, integration_id: str, entry_key: str) -> str:
        return f"gnw.{integration_id}.{entry_key}.pending_job_ids"

    async def add_pending_job(self, integration_id: str, entry_key: str, job_data: dict):
        job_id = job_data.get("job_id")
        if not job_id:
            raise ValueError("job_data must include a 'job_id' field")
        pipe = self._db.pipeline(transaction=True)
        pipe.setex(self._pending_job_key(integration_id, entry_key, job_id),
                   self.JOB_TTL_SECONDS, json.dumps(job_data, default=str))
        pipe.sadd(self._pending_jobs_set_key(integration_id, entry_key), job_id)
        await pipe.execute()

    async def get_pending_jobs(self, integration_id: str, entry_key: str) -> list:
        set_key = self._pending_jobs_set_key(integration_id, entry_key)
        job_ids = await self._db.smembers(set_key)
        jobs, expired = [], []
        for job_id in job_ids or []:
            job_id = job_id.decode("utf8") if isinstance(job_id, bytes) else job_id
            data = await self._db.get(self._pending_job_key(integration_id, entry_key, job_id))
            if data:
                jobs.append(json.loads(data))
            else:
                expired.append(job_id)
        if expired:
            await self._db.srem(set_key, *expired)
        return jobs

    async def remove_pending_job(self, integration_id: str, entry_key: str, job_id: str):
        pipe = self._db.pipeline(transaction=True)
        pipe.delete(self._pending_job_key(integration_id, entry_key, job_id))
        pipe.srem(self._pending_jobs_set_key(integration_id, entry_key), job_id)
        await pipe.execute()

    # --- window anchors (per entry) ---
    def _anchor_key(self, integration_id: str, entry_key: str) -> str:
        return f"gnw.{integration_id}.{entry_key}.window_anchor"

    async def get_window_anchor(self, integration_id: str, entry_key: str) -> Optional[str]:
        val = await self._db.get(self._anchor_key(integration_id, entry_key))
        return val.decode("utf8") if isinstance(val, bytes) else val

    async def set_window_anchor(self, integration_id: str, entry_key: str, anchor_date_iso: str):
        await self._db.set(self._anchor_key(integration_id, entry_key), anchor_date_iso)

    # --- posted-record dedup ledger (per entry) ---
    def _posted_key(self, integration_id: str, entry_key: str) -> str:
        return f"gnw.{integration_id}.{entry_key}.posted_fingerprints"

    async def filter_new_fingerprints(self, integration_id: str, entry_key: str,
                                      fingerprints: List[str], ttl_seconds: int) -> Set[str]:
        """Return the subset of fingerprints not yet marked posted, pruning
        ledger members older than ttl_seconds first."""
        key = self._posted_key(integration_id, entry_key)
        now = time.time()
        await self._db.zremrangebyscore(key, "-inf", now - ttl_seconds)
        if not fingerprints:
            return set()
        pipe = self._db.pipeline(transaction=False)
        for fp in fingerprints:
            pipe.zscore(key, fp)
        scores = await pipe.execute()
        return {fp for fp, score in zip(fingerprints, scores) if score is None}

    async def mark_fingerprints_posted(self, integration_id: str, entry_key: str,
                                       fingerprints) -> None:
        fingerprints = list(fingerprints)
        if not fingerprints:
            return
        key = self._posted_key(integration_id, entry_key)
        now = time.time()
        await self._db.zadd(key, {fp: now for fp in fingerprints})

    # --- dataset version status (per dataset, via template state manager) ---
    async def get_dataset_status(self, integration_id: str, dataset: str) -> Optional[dict]:
        return await self._manager.get_state(integration_id, "pull_events", dataset)

    async def set_dataset_status(self, integration_id: str, dataset: str, status: dict):
        await self._manager.set_state(integration_id, "pull_events", status, source_id=dataset)
