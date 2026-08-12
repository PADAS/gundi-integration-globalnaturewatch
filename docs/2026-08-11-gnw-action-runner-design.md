# Global Nature Watch Action Runner — Design

**Date:** 2026-08-11
**Status:** Approved (brainstorming complete; pending implementation plan)
**Replaces:** `gundi-integration-gfw`

## Overview

A new Gundi v2 integration, **`gundi-integration-globalnaturewatch`**, that pulls data from the GFW Data API (`data-api.globalforestwatch.org`) into Gundi Events. Where the current GFW integration hardcodes two datasets (NASA VIIRS fires, GFW integrated alerts), the new integration offers users a **curated, growable list of datasets**; for each chosen dataset the user selects which fields to pull, which filters to apply, and whether Events are created **per record** or **aggregated into H3 grid cells**.

### Goals

- Users choose datasets from a curated allowlist, with per-dataset field selection and filtering, entirely through Gundi portal configuration — no code change per user.
- Adding a dataset to the offering is a one-entry allowlist change, not a new handler.
- Per-record or H3-grid-aggregated Event output, selectable per dataset entry.
- Dynamic portal dropdowns (datasets, fields) via the **reference actions** mechanism pioneered in `gundi-integration-cmore`.
- Handler-level test coverage from day one (the old repo has none).

### Non-goals

- Not a modification of `gundi-integration-gfw` — that integration keeps running untouched until users migrate. New repo, new integration type.
- No automatic dataset discovery beyond the allowlist (considered and rejected: the API's `/datasets` returns hundreds of context layers unsuited to Events).
- No raw-SQL filter entry (considered and rejected: injection surface, poor UX).
- No aggregation strategies beyond per-record and H3 grid in v1 (the seam is pluggable; only these two are implemented).
- No event-level dedup via external IDs (matches current behavior; redundancy is bounded upstream instead — see Event semantics).

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Dataset scope | Curated allowlist maintained in the repo |
| Portal config shape | One array of dataset entries; each entry = dataset + fields + filters + output mode + event type |
| Filters | Structured rows `{field, operator, value}`, AND'd; SQL built safely from parts |
| Aggregation | Pluggable `OutputStrategy` seam; `per_record` and `h3_grid` implemented (h3-py, user-picked resolution) |
| Event types | User-editable per entry, defaulted from the allowlist spec (`_agg` suffix for aggregated mode) |
| Repo/rollout | New repo from the action-runner template; new "Global Nature Watch" integration type |
| Dynamic dropdowns | Vendor cmore's reference-actions contract; ship degraded (free-text) until portal Phase 1 lands |
| Pipeline | Hybrid: `QueryJob` interface (from "unified job" concept) with persistence only for batch jobs |

## Repo structure & provenance

Forked from `gundi-integration-action-runner` (current main). Integration type name "Global Nature Watch" set in `app/settings/integration.py`; slug injected at deploy time via `gundi-integrations-v2-infra`, as today.

Three source lineages, kept separate:

1. **Vendored from cmore, contract-identical** (to keep future upstreaming compatible):
   - `ReferenceActionConfiguration`, `ReferenceOption`, `ReferenceDataResponse` in `app/actions/core.py`
   - `ActionTypeEnum.REFERENCE` in `app/services/core.py`
   - Action-runner carve-outs in `app/services/action_runner.py`: stateless execution for reference actions (no stored config + no overrides is valid), and secret redaction (reference-action failures never attach `integration.configurations` to error events/responses)
   - `REGISTER_REFERENCE_ACTIONS` env flag, default `False`
2. **Ported from `gundi-integration-gfw`, cleaned:**
   - `DataAPI` client: username/password → bearer token → x-api-key flow; AOI/geostore resolution including `globalforestwatch.org` / `globalnaturewatch.org` URL patterns and `gfw.global` short-link redirects; sync `query/json` and batch `query/batch` paths; job polling and download handling
   - Removed in the rewrite: f-string SQL construction, the `magic_value_ignore_apikeys_before` API-key workaround, the `fields | ... or set()` precedence bug, `raise_on_giveup=False` silent-empty behavior
   - State helpers (AOI cache, pending jobs, quiet periods) live in **`app/actions/`**, not patched into the template's `app/services/state.py` (that placement caused merge conflicts on every template sync in the old repo)
3. **New code:** dataset allowlist module, safe SQL builder, `OutputStrategy` seam + two strategies, reference-action handlers, generic query-job sub-action.

Dependencies (`requirements.in`): `h3~=4.x` (new), `backoff`, `httpx` (pin per template's FastAPI compatibility). `shapely` dropped (only the old CLI used it).

## Dataset allowlist

`app/actions/datasets.py` — a checked-in registry. Each spec carries only what the API cannot report about itself:

```python
class QueryMode(str, Enum):
    SYNC = "sync"      # GET /dataset/{d}/latest/query/json, date-sliced windows
    BATCH = "batch"    # POST /dataset/{d}/latest/query/batch + job polling

class DatasetSpec(BaseModel):
    title: str
    date_field: str                     # column for date-range WHERE clause and recorded_at
    query_mode: QueryMode
    default_event_type: str
    default_fields: list[str]
    default_lookback_days: int
    lat_field: str = "latitude"         # geometry columns; always included in SELECT
    lon_field: str = "longitude"

DATASET_REGISTRY: dict[str, DatasetSpec] = {
    "nasa_viirs_fire_alerts": DatasetSpec(
        title="NASA VIIRS Fire Alerts",
        date_field="alert__date",
        query_mode=QueryMode.SYNC,
        default_event_type="gnw_viirs_fires",
        default_fields=["confidence__cat", "frp__MW"],
        default_lookback_days=10,
    ),
    "gfw_integrated_alerts": DatasetSpec(
        title="GFW Integrated Deforestation Alerts",
        date_field="gfw_integrated_alerts__date",
        query_mode=QueryMode.BATCH,
        default_event_type="gnw_integrated_alerts",
        default_fields=["gfw_integrated_alerts__confidence"],
        default_lookback_days=30,
    ),
}
```

Everything else about a dataset — its field inventory, types, filterability, descriptions, `updated_on` — comes live from the API (`GET /dataset/{d}/latest/fields`, `GET /dataset/{d}/{v}`).

The v1 allowlist ships these two datasets (parity with the old integration). Growing the offering = adding a registry entry.

## Configuration model

**Auth action** (portal-visible, executable): `email: str` + `password: SecretStr`, unchanged from the old integration. Credentials feed the token flow; the runner mints/reuses an x-api-key from the bearer token.

**Pull action** (portal-visible, periodic):

```python
class FilterRow(BaseModel):
    field: str
    operator: Literal["=", "!=", ">", ">=", "<", "<=", "in"]
    value: str          # parsed at runtime to the field's data_type; for "in", comma-separated

class PerRecordOutput(BaseModel):
    mode: Literal["per_record"] = "per_record"

class H3GridOutput(BaseModel):
    mode: Literal["h3_grid"] = "h3_grid"
    resolution: int = Field(7, ge=4, le=10)   # res 6 ≈ 36 km², 7 ≈ 5 km², 8 ≈ 0.7 km² hexes

class DatasetEntry(BaseModel):
    dataset: str                          # DATASET_REGISTRY key
    fields: list[str] = []                # extra fields beyond spec.default_fields
    filters: list[FilterRow] = []
    output: PerRecordOutput | H3GridOutput = Field(
        default_factory=PerRecordOutput, discriminator="mode")
    event_type: Optional[str] = None      # default: spec.default_event_type (+ "_agg" if h3_grid)
    lookback_days: Optional[int] = None   # default: spec.default_lookback_days

class PullEventsConfig(PullActionConfiguration):
    aoi_url: str                          # GFW/GNW share link; parsed as in the old aoi_from_url
    dataset_entries: list[DatasetEntry]
    force_fetch: bool = False             # bypasses the dataset version gate
```

The discriminated union serializes to a JSON-schema `oneOf`, which react-jsonschema-form renders as a mode dropdown that swaps in the H3 resolution field.

**Runtime config validation** (start of every pull run, per entry): dataset key in registry; every selected/filtered field exists in the dataset's live `/fields`; filter fields have `is_filter: true`; filter values parse to the field's `data_type`. Invalid entries are logged to the activity log and skipped; valid entries proceed.

## Reference actions & portal wiring

Two reference actions (config subclasses `ReferenceActionConfiguration`; the config model's fields are the query params):

- `action_list_datasets` — no params. Options from `DATASET_REGISTRY`: `value` = registry key, `label` = spec title, `description` from dataset metadata (live fetch, falling back to spec title alone on failure).
- `action_list_dataset_fields(dataset: str, filterable_only: bool = False)` — live `GET /dataset/{dataset}/latest/fields`; `value` = field name, `label` = alias, `description` = description; `filterable_only=true` restricts to `is_filter` fields. Unknown dataset → 422-style validation error, not 500 (adopts the RFC recommendation; cmore's current handlers 500 here).

Wiring in `PullEventsConfig.ui_schema()` via `gundi:reference` (never `ui:widget`):

| Field | Action | Params |
|---|---|---|
| `dataset_entries[].dataset` | `list_datasets` | — |
| `dataset_entries[].fields[]` | `list_dataset_fields` | `{"dataset": {"$data": "../../dataset"}}` |
| `dataset_entries[].filters[].field` | `list_dataset_fields` | `{"dataset": {"$data": "../../../../dataset"}, "filterable_only": true}` |

`$data` paths follow the cmore convention: paths are relative to the object containing the annotated field, and an array and its items count as separate levels. All annotations set `allow_free_text: true`.

**Portal dependency:** portal support for reference actions (Phase 1 of the cmore RFC) is not built. Until it lands, annotated fields render as plain text inputs — users type dataset keys and field names, validated at runtime with clear activity-log errors. Registration of reference actions stays behind `REGISTER_REFERENCE_ACTIONS` (default off) until the platform accepts the `"reference"` action type. No enum snapshots are baked into the schema as a bridge (considered and rejected: re-introduces static-schema machinery this design exists to avoid).

A drift-guard test (ported from cmore) asserts every `gundi:reference` annotation names a registered reference action, declares params that are a subset of the query model's fields, covers all required params, and never sets `ui:widget`.

## Fetch pipeline

### Orchestrator — `action_pull_events` (crontab `*/10 * * * *`)

1. Resolve `aoi_url` → AOI → geostore IDs through the state cache (7-day TTL; live fetch on miss).
2. Validate entries (see Configuration model). Per-entry isolation: one bad entry never stalls the rest.
3. Per entry, consult its **quiet period** — Redis TTL key, tiered as in the old integration: long randomized period after productive work (events were posted), short when idle, none while batch jobs are pending. Keyed by a **content hash of the entry** (dataset + fields + filters + output + event_type), so duplicate entries for the same dataset with different filters pace independently, and reordering entries does not disturb state.
4. Per entry, check the **dataset version gate**: fetch `GET /dataset/{d}/latest` metadata; skip the entry if `updated_on` has not advanced past the stored `DatasetStatus.latest_updated_on` (keyed per dataset, shared across entries). `force_fetch` bypasses.
5. Fan out work as `QueryJob` descriptors via `trigger_action` to the generic sub-action:
   - **Sync entries:** one job per (geostore batch × date-window slice); windows generated as in the old `generate_date_pairs` (7-day slices), each queried as a half-open `[start, end)` SQL interval. `end` is always the next UTC midnight. `start` is the entry's stored window anchor, clamped in both directions every run: never older than `end - lookback_days` (so a stuck or ancient anchor can't force a permanent full-lookback re-pull once it falls behind the lookback window), and never newer than `end - reingest_margin_days` (`DatasetSpec.reingest_margin_days`: how far the dataset's own ingestion pipeline lags behind real time — e.g. VIIRS ingests through day D and into D+1; integrated alerts lag days-to-weeks — so a same-day anchor still re-covers data the provider hasn't finished backfilling). The effective `start` is persisted as the new anchor *before* fan-out; `action_run_query_job` only advances the anchor when the window it just completed started exactly at that stored anchor, so a failed or out-of-order slice is re-covered next run instead of silently advanced past. Consequence: records inside the reingest margin can be re-queried (and, absent event-level dedup, re-posted) on every run that opens a new dataset version — a bounded, deliberate at-least-once tradeoff in exchange for never permanently losing late-arriving data.
   - **Batch entries:** first collect completed pending jobs (poll → download → post → remove from pending set), then submit one new batch job covering the lookback window across all geostores.

### The job seam

```python
class JobState(str, Enum):
    PENDING = "pending"; RUNNING = "running"; RESULTS_READY = "results_ready"
    POSTED = "posted"; FAILED = "failed"; EXPIRED = "expired"

class QueryJob(Protocol):
    async def start(self) -> JobState     # SyncQueryJob: executes query/json inline → RESULTS_READY
    async def check(self) -> JobState     # BatchQueryJob: polls the API job link
    async def collect(self) -> list[dict] # rows (sync) / download + flatten results (batch)
```

- `SyncQueryJob` — no persistence; `start()` runs the query to completion.
- `BatchQueryJob` — `start()` POSTs to `query/batch` and persists a pending-job record (Redis, TTL'd set-indexed keys, as in the old `state.py` helpers, relocated to `app/actions/`). `check()` polls the job link; `collect()` handles the signed download link, detecting expiry from the `Expires` query param and re-polling for a fresh link before fetching.

Everything downstream of `collect()` is a single shared funnel.

### Generic sub-action — `action_run_query_job` (internal, not portal-visible)

Takes `RunQueryJobConfig` (`InternalActionConfiguration`): entry config + geostore IDs + window + job kind. Reconstructs the `QueryJob`, runs it under the module-level `asyncio.Semaphore` (`GNW_DATASET_QUERY_CONCURRENCY`, default 5; the API's practical ceiling is ~50 concurrent requests across all instances), then: rows → `OutputStrategy.to_events()` → `send_events_to_gundi`. Replaces all four dataset-specific handlers of the old repo.

### SQL builder — `build_query(spec, entry, window, validated_fields) -> str`

- SELECT list: `spec.lat_field`, `spec.lon_field`, `spec.date_field`, `spec.default_fields`, `entry.fields` — every identifier checked against the dataset's `/fields` inventory (identifier allowlisting, not escaping).
- WHERE: `{date_field} >= '{window.start}' AND {date_field} < '{window.end}'` (half-open — the upper bound is exclusive, so consecutive windows that share an edge date never both match it) with dates rendered from `date` objects, AND'd with each filter row rendered as `identifier operator typed-literal`. String literals are escaped and quoted; numeric/boolean literals rendered from parsed values; `in` renders a parenthesized literal list.
- No user-supplied string is ever interpolated into SQL unvalidated.

## Output strategies

```python
class OutputStrategy(Protocol):
    def to_events(self, rows: list[dict], entry: DatasetEntry, spec: DatasetSpec) -> list[dict]: ...

OUTPUT_STRATEGIES = {"per_record": PerRecordStrategy(), "h3_grid": H3GridStrategy()}
```

**`PerRecordStrategy`** — one Event per row:
- `location`: from `spec.lat_field` / `spec.lon_field`
- `recorded_at`: the row's `spec.date_field` value
- `title`: `spec.title`; `event_type`: resolved from entry/spec
- `event_details`: every selected field, verbatim

**`H3GridStrategy`** — one Event per non-empty cell per run:
- Bucket rows by `h3.latlng_to_cell(lat, lon, entry.output.resolution)`
- `location`: cell centroid (`h3.cell_to_latlng`)
- `recorded_at`: newest record date in the cell
- `title`: `"{spec.title} — {count} records"`
- `event_details`: `record_count`, `cell_id` (H3 index string), `resolution`, and `min`/`max`/`mean` for each numeric selected field (non-numeric fields omitted in aggregate mode)

Adding a strategy later = one class + one config model in the `output` union + one registry entry; handlers and fetch logic are untouched.

**Posted-record ledger and H3 counts.** Rows are filtered through the posted-record dedup ledger (see "Event semantics & dedup") *before* `to_events` runs, so `record_count` (and the min/max/mean fields) for an H3 cell reflect only the NEW, not-yet-posted rows seen in that run — not the cell's full record history. A cell whose rows have already all been posted in a prior run produces no Event at all this run.

## Event semantics & dedup

No event-level external IDs (matches the old integration; Gundi/ER-side dedup is out of scope) — dedup instead happens one layer up, via the posted-record ledger described below. Double-sends are bounded, not eliminated:
- The **dataset version gate** ensures a dataset is only re-fetched after the API reports new data.
- **Quiet periods** pace runs per entry.
- **Windows are half-open** (`[start, end)`), so two windows that share a boundary date never both match it — the boundary-day double-count that an inclusive-both-ends interval would produce on multi-window catch-up runs is structurally impossible.
- **Windows re-cover the reingest margin, not just the anchor.** Each run's `start` is clamped to `end - reingest_margin_days`, so every run re-queries the dataset's ingestion-lag window even when the stored anchor is more recent. This still means those records are re-fetched once per dataset-version bump within the margin — but a Redis-backed posted-record ledger (per entry, sorted-set of `row_fingerprint` values with score = posting time, TTL = `reingest_margin_days + 7` days) filters out rows already posted before Events are built, so re-fetching the margin no longer means re-posting it. Net semantics: **at-most-once within the ledger TTL, at-least-once overall** — if the ledger itself is lost, behavior degrades to the old bounded-duplication tradeoff (re-post within the margin), never to permanently missing late-arriving data. Only a row whose fingerprint (a hash of *all* its field values) has genuinely changed — e.g. an integrated-alerts confidence upgrade — is treated as new and posted again, by design.
- Batch jobs are removed from the pending set only after their results post successfully; a job that fails to post is retried on the next run, which is the same deliberate at-least-once seam (identical to the old integration).

## Error handling

- **Query failures raise** after capped backoff (the old client's `raise_on_giveup=False` made failures indistinguishable from empty results). A failed sub-action is recorded by the activity logger; the entry's quiet period stays short so it retries soon.
- **Per-entry isolation** in the orchestrator: config or fetch errors for one entry are logged and skipped; other entries run.
- **Reference actions**: validation failures → 422-style errors; errors never include integration configuration (vendored redaction carve-out).
- **Batch-job hygiene**: expired download links refreshed via re-poll; vanished/expired jobs removed from the pending set with an activity-log warning; their window is covered by the next submitted job.
- **Auth failures** raise immediately so runs are visibly failed, never silently empty.

## State (all Redis via the template state manager, keys owned by `app/actions/`)

| State | Key basis | TTL |
|---|---|---|
| AOI/geostore cache | integration + AOI id | 7 days |
| Quiet period | integration + entry content hash | tiered (0 / short / long, randomized) |
| Dataset version gate (`DatasetStatus`) | integration + dataset key | none (overwritten) |
| Pending batch jobs | integration + entry content hash + job id (set-indexed) | job TTL |
| Last-pull window anchor | integration + entry content hash | none (overwritten) |

## Testing

- **Client tests** (`respx`): token/API-key flow, sync query, batch submit/poll/download (incl. expired-link refresh), AOI URL parsing for both domains and `gfw.global` redirects.
- **Handler tests** (template fixtures: `mock_gundi_client_v2`, `mock_state_manager`, `mock_publish_event`, `TRIGGER_ACTIONS_ALWAYS_SYNC=true`): quiet-period skip, version-gate skip and `force_fetch` bypass, per-entry error isolation, sync path end-to-end (rows → events posted), batch submit/collect cycle, window anchoring.
- **Unit tests**: SQL builder (identifier rejection, hostile filter values, type rendering, `in` lists); both output strategies (H3 bucketing/centroids/stats against known coordinates); entry content-hash stability under reordering.
- **Reference-action tests + drift guard**, ported from cmore.
- **Committed `/fields` fixtures** per allowlisted dataset (field names vary across dataset versions; validation logic must be tested against real inventories).

## Rollout

1. Repo created from template; parity allowlist (two datasets); deployed as a new integration type alongside the old GFW integration.
2. `REGISTER_REFERENCE_ACTIONS=false` until the platform accepts the `"reference"` action type; config forms work as validated free text meanwhile, and become dropdowns with zero integration-side changes when portal Phase 1 ships.
3. Existing GFW users migrate by creating a Global Nature Watch integration; the `event_type` fields let them keep their existing ER event types (`gfwfirealert`, `gfwgladalert`) during migration if desired.
4. Old integration decommissioned once migrations complete (out of scope for this spec).
