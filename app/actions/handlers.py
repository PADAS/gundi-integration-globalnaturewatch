import asyncio
import logging
import random
from datetime import date, datetime, timedelta, timezone

import httpx
import pydantic

import app.settings
from app.actions.configurations import (
    AuthenticateConfig, ListDatasetsQuery, ListDatasetFieldsQuery, PullEventsConfig,
    RunQueryJobConfig, entry_state_key, get_auth_config,
)
from app.actions.core import ReferenceDataResponse, ReferenceOption
from app.actions.datasets import DATASET_REGISTRY, QueryMode
from app.actions.gnwclient import (
    AOIData, DataAPI, DataAPIAuthException, DatasetStatus, DownloadLinkExpiredException,
)
from app.actions.jobs import BatchQueryJob, JobState, SyncQueryJob, generate_windows
from app.actions.output import OUTPUT_STRATEGIES
from app.actions.sqlbuilder import ConfigValidationError, build_query
from app.actions.state import GnwState
from app.services.action_scheduler import crontab_schedule, trigger_action
from app.services.activity_logger import activity_logger, log_action_activity
from app.services.gundi import send_events_to_gundi
from app.services.state import IntegrationStateManager
from gundi_core.schemas.v2 import Integration, LogLevel

logger = logging.getLogger(__name__)

state_manager = IntegrationStateManager()
gnw_state = GnwState(state_manager)
sema = asyncio.Semaphore(app.settings.GNW_DATASET_QUERY_CONCURRENCY)


async def action_list_datasets(integration, action_config: ListDatasetsQuery):
    """Reference action: dataset options for the portal's entry picker."""
    descriptions = {}
    try:  # /datasets is unauthenticated; enrich labels with live metadata
        for ds in await DataAPI(username=None, password=None).get_datasets():
            if ds.dataset in DATASET_REGISTRY and ds.metadata and ds.metadata.overview:
                descriptions[ds.dataset] = ds.metadata.overview[:300]
    except Exception:
        logger.warning("Could not fetch live dataset metadata; serving registry only.", exc_info=True)
    options = [
        ReferenceOption(value=key, label=spec.title, description=descriptions.get(key))
        for key, spec in DATASET_REGISTRY.items()
    ]
    return ReferenceDataResponse(options=options).dict()


async def action_list_dataset_fields(integration, action_config: ListDatasetFieldsQuery):
    """Reference action: field options for a chosen dataset (cascaded via $data)."""
    fields = await DataAPI(username=None, password=None).get_dataset_fields(
        dataset=action_config.dataset
    )
    if action_config.filterable_only:
        fields = [f for f in fields if f.is_filter]
    options = [
        ReferenceOption(
            value=f.name, label=f.alias or f.name,
            description=f.description if isinstance(f.description, str) else None,
        )
        for f in fields
    ]
    return ReferenceDataResponse(options=options).dict()


async def _post_rows(rows, entry, spec, integration_id) -> int:
    """Shared funnel: rows from any query path -> output strategy -> Gundi Events."""
    strategy = OUTPUT_STRATEGIES[entry.output.mode]
    events = strategy.to_events(rows, entry, spec)
    if events:
        await send_events_to_gundi(events=events, integration_id=integration_id)
    return len(events)


async def _advance_anchor_contiguously(integration_id, key, window_start: str, window_end: str):
    """Advance only when this window starts exactly at the stored anchor, so a
    failed earlier slice is re-covered next run (at-least-once semantics)."""
    anchor = await gnw_state.get_window_anchor(integration_id, key)
    if anchor is None or anchor == window_start:
        await gnw_state.set_window_anchor(integration_id, key, window_end)


async def action_run_query_job(integration: Integration, action_config: RunQueryJobConfig):
    """Internal sub-action: fetch one dataset entry's window (sync or batch)
    and funnel resulting rows into Gundi Events. Triggered by the pull_events
    orchestrator (Task 13), one call per (entry, window) slice."""
    entry = action_config.entry
    spec = DATASET_REGISTRY[entry.dataset]
    key = entry_state_key(entry)
    integration_id = str(integration.id)
    auth = get_auth_config(integration)
    client = DataAPI(username=auth.email, password=auth.password.get_secret_value())

    result = {"dataset": entry.dataset, "rows_fetched": 0, "events_posted": 0,
              "jobs_submitted": 0, "jobs_completed": 0, "jobs_pending": 0, "jobs_failed": 0}

    dataset_fields = await client.get_dataset_fields(dataset=entry.dataset)
    sql = build_query(
        spec=spec, extra_fields=entry.fields, filters=[f.dict() for f in entry.filters],
        window_start=action_config.window_start, window_end=action_config.window_end,
        dataset_fields=dataset_fields,
    )

    if spec.query_mode == QueryMode.SYNC:
        rows = []
        for geostore_id in action_config.geostore_ids:
            job = SyncQueryJob(client, dataset=entry.dataset, sql=sql, geostore_id=geostore_id)
            async with sema:
                await job.start()
            rows.extend(job.collect())
        result["rows_fetched"] = len(rows)
        result["events_posted"] = await _post_rows(rows, entry, spec, integration_id)
        await _advance_anchor_contiguously(
            integration_id, key,
            action_config.window_start.isoformat(), action_config.window_end.isoformat(),
        )
        quiet = random.randint(120, 240) if result["events_posted"] else random.randint(30, 60)
        await gnw_state.set_quiet_period(integration_id, key, timedelta(minutes=quiet))
        return result

    # BATCH mode: poll pending jobs first. Each phase (check / collect / post /
    # cleanup) is handled separately so a failure after events have already
    # been posted is never silently swallowed as "still pending" — that would
    # cause the job to be re-collected and re-posted next run (double-post).
    for job_data in await gnw_state.get_pending_jobs(integration_id, key):
        job_id = job_data.get("job_id")
        job = BatchQueryJob(client, dataset=entry.dataset, sql=sql,
                            geostore_ids=action_config.geostore_ids)
        try:
            state, download_link, message = await job.check(job_data["job_link"])
        except Exception:
            logger.exception(f"Error polling job {job_id}")
            result["jobs_pending"] += 1
            continue

        if state == JobState.RESULTS_READY:
            if message == "partial_success":
                await log_action_activity(
                    integration_id=integration.id, action_id="run_query_job",
                    level=LogLevel.WARNING,
                    title=f"Batch job {job_id} completed with partial success",
                    data=job_data)
            try:
                rows = await job.collect(download_link)
            except DownloadLinkExpiredException as e:
                await log_action_activity(
                    integration_id=integration.id, action_id="run_query_job",
                    level=LogLevel.ERROR,
                    title=f"Batch job {job_id} download link expired",
                    data={**job_data, "error": str(e)})
                result["jobs_failed"] += 1
                await gnw_state.remove_pending_job(integration_id, key, job_id)
                continue
            try:
                result["rows_fetched"] += len(rows)
                result["events_posted"] += await _post_rows(rows, entry, spec, integration_id)
            except Exception:
                # Post failed: keep the job pending so the next run retries (at-least-once).
                logger.exception(f"Failed posting results of job {job_id}; leaving pending for retry")
                result["jobs_pending"] += 1
                continue
            result["jobs_completed"] += 1
            try:
                await gnw_state.remove_pending_job(integration_id, key, job_id)
                if job_data.get("window_start") and job_data.get("window_end"):
                    await _advance_anchor_contiguously(
                        integration_id, key, job_data["window_start"], job_data["window_end"])
            except Exception:
                # Events already posted; job may be re-collected next run. Loud, not silent.
                logger.exception(f"State cleanup failed after posting job {job_id}")
                await log_action_activity(
                    integration_id=integration.id, action_id="run_query_job",
                    level=LogLevel.ERROR,
                    title=f"Batch job {job_id}: cleanup failed after posting; duplicate events possible on next poll",
                    data=job_data)
        elif state == JobState.FAILED:
            await log_action_activity(
                integration_id=integration.id, action_id="run_query_job",
                level=LogLevel.ERROR,
                title=f"Batch job {job_id} failed",
                data={**job_data, "message": message})
            result["jobs_failed"] += 1
            await gnw_state.remove_pending_job(integration_id, key, job_id)
        else:
            result["jobs_pending"] += 1

    if action_config.submit_new:
        job = BatchQueryJob(client, dataset=entry.dataset, sql=sql,
                            geostore_ids=action_config.geostore_ids)
        async with sema:
            await job.start()
        await gnw_state.add_pending_job(integration_id, key, {
            **job.job_record,
            "window_start": action_config.window_start.isoformat(),
            "window_end": action_config.window_end.isoformat(),
        })
        result["jobs_submitted"] += 1

    if result["jobs_pending"]:
        quiet_minutes = 0
    elif result["jobs_submitted"] or result["jobs_completed"]:
        quiet_minutes = random.randint(240, 720)
    else:
        quiet_minutes = random.randint(30, 60)
    await gnw_state.set_quiet_period(integration_id, key, timedelta(minutes=quiet_minutes))
    return result


async def action_auth(integration, action_config: AuthenticateConfig):
    """Validate a user-supplied email/password against the GFW Data API."""
    try:
        client = DataAPI(username=action_config.email,
                         password=action_config.password.get_secret_value())
        token = await client.get_access_token()
    except (DataAPIAuthException, httpx.HTTPError) as e:
        return {"valid_credentials": False,
                "message": f"Failed to authenticate with the GFW Data API: {e}"}
    return {"valid_credentials": token is not None}


@activity_logger()
@crontab_schedule("*/10 * * * *")
async def action_pull_events(integration: Integration, action_config: PullEventsConfig):
    """Scheduled orchestrator: resolve the AOI once, then for each configured
    dataset entry validate config, respect quiet periods/version gates, and
    trigger `run_query_job` (Task 14) for the windows that still need data.
    Per-entry isolation: one bad entry never stops the others from running."""
    integration_id = str(integration.id)
    result = {"triggered": [], "skipped": [], "errors": []}
    auth = get_auth_config(integration)
    client = DataAPI(username=auth.email, password=auth.password.get_secret_value())

    # --- AOI: cached-first, live fallback, cache on success ---
    cached = await gnw_state.get_aoi_data(integration_id)
    if cached:
        aoi_data = AOIData.parse_obj(cached)
    else:
        try:
            aoi_id = await client.aoi_from_url(str(action_config.aoi_url))
            aoi_data = await client.get_aoi(aoi_id=aoi_id)
            await gnw_state.set_aoi_data(integration_id, aoi_data.dict())
        except Exception as e:
            msg = f"Failed to resolve AOI from {action_config.aoi_url}: {e}"
            await log_action_activity(integration_id=integration.id, action_id="pull_events",
                                      level=LogLevel.ERROR, title=msg, data={"error": str(e)})
            result["errors"].append(msg)
            return result
    if not aoi_data.attributes.geostore:
        msg = f"No Geostore associated with AOI {aoi_data.id}."
        await log_action_activity(integration_id=integration.id, action_id="pull_events",
                                  level=LogLevel.ERROR, title=msg, data={"aoi_id": aoi_data.id})
        result["errors"].append(msg)
        return result
    geostore_ids = [aoi_data.attributes.geostore]

    for index, entry in enumerate(action_config.dataset_entries):
        label = f"entry[{index}]:{entry.dataset}"
        try:
            spec = DATASET_REGISTRY.get(entry.dataset)
            if spec is None:
                raise ConfigValidationError(
                    [f"Dataset '{entry.dataset}' is not offered. Available: {sorted(DATASET_REGISTRY)}"])
            key = entry_state_key(entry)

            # validate fields/filters against live inventory (build once, discard)
            dataset_fields = await client.get_dataset_fields(dataset=entry.dataset)
            probe_day = datetime.now(tz=timezone.utc).date()
            build_query(spec=spec, extra_fields=entry.fields,
                        filters=[f.dict() for f in entry.filters],
                        window_start=probe_day, window_end=probe_day,
                        dataset_fields=dataset_fields)

            # batch entries with pending jobs always poll (download links expire)
            if spec.query_mode == QueryMode.BATCH:
                if await gnw_state.get_pending_jobs(integration_id, key):
                    end = _next_midnight_utc()
                    start = end - timedelta(days=entry.resolved_lookback_days(spec))
                    await trigger_action(integration.id, "run_query_job", config=RunQueryJobConfig(
                        entry=entry, geostore_ids=geostore_ids,
                        window_start=start.date(), window_end=end.date(), submit_new=False))
                    result["triggered"].append(f"{label} (polling jobs)")
                    continue

            if not action_config.force_fetch and await gnw_state.is_quiet_period(integration_id, key):
                result["skipped"].append(f"{label} (quiet period)")
                continue

            # version gate (per dataset)
            metadata = await client.get_dataset_metadata(entry.dataset)
            stored = await gnw_state.get_dataset_status(integration_id, entry.dataset)
            if stored and not action_config.force_fetch:
                try:
                    status = DatasetStatus.parse_obj(stored)
                    if status.latest_updated_on >= metadata.updated_on:
                        await gnw_state.set_quiet_period(
                            integration_id, key, timedelta(minutes=random.randint(30, 60)))
                        result["skipped"].append(f"{label} (no dataset updates)")
                        continue
                except pydantic.ValidationError:
                    logger.warning(f"Discarding invalid stored dataset status for {entry.dataset}")

            # window: anchor-first
            end = _next_midnight_utc().date()
            lookback_start = end - timedelta(days=entry.resolved_lookback_days(spec))
            anchor = await gnw_state.get_window_anchor(integration_id, key)
            start = max(date.fromisoformat(anchor), lookback_start) if anchor else lookback_start
            if start >= end:
                result["skipped"].append(f"{label} (window empty)")
                continue

            if spec.query_mode == QueryMode.SYNC:
                for window_start, window_end in generate_windows(start, end, interval_days=7):
                    await trigger_action(integration.id, "run_query_job", config=RunQueryJobConfig(
                        entry=entry, geostore_ids=geostore_ids,
                        window_start=window_start, window_end=window_end))
            else:
                await trigger_action(integration.id, "run_query_job", config=RunQueryJobConfig(
                    entry=entry, geostore_ids=geostore_ids,
                    window_start=start, window_end=end, submit_new=True))
            result["triggered"].append(label)

            await gnw_state.set_dataset_status(integration_id, entry.dataset, DatasetStatus(
                dataset=metadata.dataset, version=metadata.version,
                latest_updated_on=metadata.updated_on,
            ).dict())

        except ConfigValidationError as e:
            msg = f"{label}: invalid configuration — {e}"
            await log_action_activity(integration_id=integration.id, action_id="pull_events",
                                      level=LogLevel.WARNING, title=msg, data={"errors": e.errors})
            result["errors"].append(msg)
        except Exception as e:
            msg = f"{label}: failed — {e}"
            logger.exception(msg)
            await log_action_activity(integration_id=integration.id, action_id="pull_events",
                                      level=LogLevel.WARNING, title=msg, data={"error": str(e)})
            result["errors"].append(msg)

    return result


def _next_midnight_utc() -> datetime:
    return (datetime.now(tz=timezone.utc) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
