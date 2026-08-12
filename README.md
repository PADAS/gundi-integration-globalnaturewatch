# gundi-integration-globalnaturewatch
Gundi v2 integration for Global Nature Watch — pulls curated GFW Data API datasets into Gundi Events. (Replaces `gundi-integration-gfw`.)

## What this is

This integration polls the [GFW Data API](https://data-api.globalforestwatch.org) (`data-api.globalforestwatch.org`) for one or more curated datasets scoped to a user's AOI, and forwards the results to Gundi as Events. It supersedes `gundi-integration-gfw`, which hardcoded exactly two datasets (NASA VIIRS fire alerts and GFW integrated deforestation alerts) with no per-user field or filter control.

Compared to the old integration:
- Users choose which datasets to pull from a **curated allowlist** (see below), entirely through Gundi portal configuration — no code change per user.
- Each dataset entry lets the user pick extra fields, apply filters, and choose whether Events are created **per record** or **aggregated into H3 grid cells**.
- A single generic fetch pipeline (`action_run_query_job`) replaces the old repo's four dataset-specific handlers.
- Handler-level test coverage from day one.

The full design rationale and decisions live in [`docs/2026-08-11-gnw-action-runner-design.md`](docs/2026-08-11-gnw-action-runner-design.md).

Existing GFW-integration users migrate by creating a new Global Nature Watch integration; the per-entry `event_type` field lets them keep existing ER event types (e.g. `gfwfirealert`, `gfwgladalert`) during migration.

## Dataset allowlist

Datasets offered to users are a checked-in registry, `DATASET_REGISTRY` in [`app/actions/datasets.py`](app/actions/datasets.py). Each entry carries only what the Data API can't report about itself:

```python
DATASET_REGISTRY: Dict[str, DatasetSpec] = {
    "nasa_viirs_fire_alerts": DatasetSpec(
        title="NASA VIIRS Fire Alerts",
        date_field="alert__date",
        query_mode=QueryMode.SYNC,           # or QueryMode.BATCH
        default_event_type="gnw_viirs_fires",
        default_fields=["confidence__cat", "frp__MW"],
        default_lookback_days=10,
        reingest_margin_days=2,              # how far this dataset's ingestion lags real time
    ),
    ...
}
```

Everything else about a dataset — field inventory, types, filterability, descriptions — comes live from the API (`GET /dataset/{dataset}/latest/fields`, `GET /dataset/{dataset}/{version}`).

`reingest_margin_days` bounds how far behind "now" each run re-queries, regardless of where the stored window anchor sits (see "Windows & the reingest margin" below) — set it to how long the dataset's own ingestion pipeline lags behind real-world events (VIIRS: ~2 days; GFW integrated alerts: ~14 days, since they aggregate multiple slower-arriving alert systems).

### Adding a dataset to the offering

1. Add one entry to `DATASET_REGISTRY` with the dataset's slug, its date field, query mode (`sync` for the inline `query/json` endpoint, `batch` for `query/batch` + job polling), a default event type, default fields, a default lookback window, and a reingest margin (how many days behind real time the dataset's own ingestion lags).
2. Add a committed `/fields` fixture so the field-inventory validation logic has real data to test against (field names and availability drift across dataset versions). Fetch the live inventory and drop it into [`app/actions/tests/fields_fixtures.py`](app/actions/tests/fields_fixtures.py):
   ```bash
   curl -s https://data-api.globalforestwatch.org/dataset/<dataset-slug>/latest/fields | python -m json.tool
   ```
3. Run the tests — `test_spec_defaults_exist_in_fields_fixtures` (in `app/actions/tests/test_datasets.py`) fails loudly if the registry references a `date_field`/`lat_field`/`lon_field`/default field that doesn't exist in the fixture, catching allowlist/API drift.

No new handler is needed — the generic pipeline (`action_pull_events` → `action_run_query_job`) drives every dataset in the registry.

## Configuration model

**Auth action** (`action_auth`, portal-visible): `email` + `password` (`SecretStr`) for the GFW Data API account. Credentials feed the token flow; the runner mints an x-api-key from the bearer token.

**Pull action** (`action_pull_events`, portal-visible, scheduled `*/10 * * * *`): `PullEventsConfig` in [`app/actions/configurations.py`](app/actions/configurations.py).

```python
class PullEventsConfig(PullActionConfiguration):
    aoi_url: HttpUrl                      # GFW/GNW AOI share link
    dataset_entries: List[DatasetEntry]
    force_fetch: bool = False             # bypasses the dataset version gate
```

Each `DatasetEntry`:

| Field | Meaning |
|---|---|
| `dataset` | Key into `DATASET_REGISTRY` |
| `fields` | Extra fields beyond the dataset's defaults |
| `filters` | Rows of `{field, operator, value}`, AND'd together; built into a safe, identifier-allowlisted SQL `WHERE` clause |
| `output` | `{"mode": "per_record"}` — one Event per row, or `{"mode": "h3_grid", "resolution": 4-10}` — rows bucketed into H3 cells, one Event per non-empty cell per run, with `record_count` and per-field min/max/mean in `event_details` |
| `event_type` | Optional override; defaults to the dataset's `default_event_type` (`_agg` suffix appended in `h3_grid` mode) |
| `lookback_days` | Optional override of the dataset's default lookback window |

`output` is a Pydantic discriminated union on the `mode` field, which serializes to a JSON-schema `oneOf` (with a `discriminator` block) that react-jsonschema-form renders as a mode dropdown — picking `h3_grid` swaps in the resolution field.

Runtime validation happens at the start of every pull run, per entry: the dataset key must be in the registry, every selected/filtered field must exist in that dataset's live `/fields`, filter fields must be filterable, and filter values must parse to the field's declared type. Invalid entries are logged to the activity log and skipped; the rest of the entries still run (per-entry isolation).

**Internal sub-action**: `action_run_query_job` (`RunQueryJobConfig`, an `InternalActionConfiguration`) is not portal-visible. It's triggered by `action_pull_events` once per (entry, window) slice, and does the actual query → output-strategy → `send_events_to_gundi` work under a shared concurrency semaphore.

### Windows & the reingest margin

Each entry's queried window is `[start, end)` — half-open, so a shared boundary date between two consecutive windows is never queried twice. `end` is always the next UTC midnight; `start` is the entry's stored **window anchor**, clamped in two directions every run:

- Up to `lookback_start` (`end - lookback_days`), so a stuck or ancient anchor can never force a full-lookback re-pull forever.
- Down to `end - reingest_margin_days`, so even a same-day anchor still re-covers the dataset's ingestion lag — the provider may still be backfilling `end`'s data days after `end` passes.

The effective `start` is persisted as the new anchor *before* any sub-action runs, and `action_run_query_job` only advances the anchor when the window it just finished started exactly at that stored anchor — so a failed or out-of-order slice is safely re-covered next run instead of silently skipped. The tradeoff: records within the reingest margin are re-queried on every run that opens a new dataset version — but a Redis-backed posted-record ledger (per entry, TTL = `reingest_margin_days + 7` days) filters out rows already posted before Events are built, so re-querying the margin no longer means re-posting it. Duplicates in EarthRanger now occur only if the ledger itself is lost (e.g. a Redis flush) or two runs for the same entry race the filter→post→mark check-then-act concurrently (batch polling overlapping a redelivered at-least-once trigger), both of which degrade to today's bounded re-posts rather than ever losing data — at-most-once within the ledger TTL under normal operation, at-least-once overall (never exactly-once). See the design doc's "Event semantics & dedup" section for the ledger's fingerprinting rules (including that two genuinely distinct records identical on every selected field collapse to one post) and the H3-aggregate count implication (`record_count` reflects only new, not-yet-posted records for that run).

**Reference actions** (`action_list_datasets`, `action_list_dataset_fields`) are also not portal-configurable in the usual sense — see below.

## Reference actions & dynamic dropdowns

The dataset picker and field pickers *want* to be portal dropdowns backed by live data, using the reference-actions mechanism vendored from `gundi-integration-cmore` (`ReferenceActionConfiguration`, `gundi:reference` UI annotations, `$data`-bound params). `PullEventsConfig.ui_schema()` wires:

- `dataset_entries[].dataset` → `list_datasets` (no params)
- `dataset_entries[].fields[]` → `list_dataset_fields`, `dataset` bound via `$data` to the sibling entry's `dataset`
- `dataset_entries[].filters[].field` → `list_dataset_fields` with `filterable_only: true`

**The Gundi portal does not support the `"reference"` action type yet** (Phase 1 of the cmore RFC is unbuilt). Until it lands:
- These fields render as plain free-text inputs (`allow_free_text: true` is always set) — users type dataset keys and field names directly, and get validated at runtime with clear activity-log errors on mistakes.
- Registration of the two reference actions with Gundi is gated behind `REGISTER_REFERENCE_ACTIONS` (env var, **default `False`**), so self-registration never sends an action type the platform would reject. Flip it on once the portal accepts `"reference"` actions — no other integration-side change is needed for the dropdowns to start working.

A drift-guard test (`test_gundi_reference_annotations_match_registered_reference_actions` in `app/actions/tests/test_configurations.py`) asserts every `gundi:reference` annotation names a registered reference action, only declares params that exist on that action's config model, covers all of that model's required params, and never also sets `ui:widget`.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `INTEGRATION_TYPE_NAME` | `Global Nature Watch` | Integration type name shown in the portal |
| `GNW_DATASET_QUERY_CONCURRENCY` | `5` | Caps concurrent requests to the GFW Data API per instance (module-level `asyncio.Semaphore` in `app/actions/handlers.py`). The API's practical ceiling is ~50 concurrent requests across all instances — size this against instance count. |
| `REGISTER_REFERENCE_ACTIONS` | `False` | Whether to self-register `list_datasets`/`list_dataset_fields` as `"reference"`-type actions with Gundi. Leave off until the portal supports that action type (see above). |

Set these in the environment or `.env` file the runner loads at startup (see `app/settings/`).

## Running tests

```bash
pip install -r requirements-base.in -r requirements-dev.in -r requirements.in  # or use the compiled requirements.txt
pytest
```

Test layout, by concern:
- `app/actions/tests/test_gnwclient.py` — Data API client (`respx`-mocked): token/API-key flow, sync query, batch submit/poll/download incl. expired-link refresh, AOI URL parsing.
- `app/actions/tests/test_handlers.py` — orchestrator and sub-action behavior: quiet-period skip, version-gate skip and `force_fetch` bypass, per-entry error isolation, sync/batch fetch cycles, window anchoring.
- `app/actions/tests/test_sqlbuilder.py`, `test_output.py`, `test_state.py`, `test_jobs.py`, `test_datasets.py`, `test_configurations.py` — unit tests for the SQL builder, output strategies, state helpers, job seam, dataset registry, and config models (including the reference-action drift guard).
- `app/actions/tests/test_reference_actions.py` — the two reference actions themselves.
- `app/actions/tests/test_registration_smoke.py` — end-to-end sanity checks that all five actions register, their portal-facing config schemas serialize, `run_query_job` is internal-only, and `pull_events` carries its crontab schedule.
- `app/services/tests/` — generic action-runner framework tests inherited from the template.

## Usage (template mechanics)
- Implement additional actions in `app/actions/handlers.py`.
- Define configurations needed for actions in `app/actions/configurations.py`.
- Or implement a webhooks handler in `app/webhooks/handlers.py`, with configurations in `app/webhooks/configurations.py`.
- Optionally, add the `@activity_logger()` decorator in actions to log common events which you can later see in the portal:
    - Action execution started
    - Action execution complete
    - Error occurred during action execution
- Optionally, add the `@webhook_activity_logger()` decorator in the webhook handler to log common events which you can later see in the portal:
    - Webhook execution started
    - Webhook execution complete
    - Error occurred during webhook execution
- Optionally, use  `log_action_activity()` or `log_webhook_activity()` to log custom messages which you can later see in the portal
- Optionally, use  `@crontab_schedule()` or `register.py --schedule` to make an action to run on a custom schedule


## Action Examples: 

```python
# actions/configurations.py
from .core import PullActionConfiguration


class PullObservationsConfiguration(PullActionConfiguration):
    lookback_days: int = 10


```

```python
# actions/handlers.py
from app.services.activity_logger import activity_logger, log_activity
from app.services.gundi import send_observations_to_gundi
from app.services.utils import crontab_schedule
from gundi_core.events import LogLevel
from .configurations import PullObservationsConfiguration


@crontab_schedule("0 */4 * * *")  # Run every 4 hours
@activity_logger()
async def action_pull_observations(integration, action_config: PullObservationsConfiguration):
    
    # Add your business logic to extract data here...
    
    # Optionally, log a custom messages to be shown in the portal
    await log_activity(
        integration_id=integration.id,
        action_id="pull_observations",
        level=LogLevel.INFO,
        title="Extracting observations with filter..",
        data={"start_date": "2024-01-01", "end_date": "2024-01-31"},
        config_data=action_config.dict()
    )
    
    # Normalize the extracted data into a list of observations following to the Gundi schema:
    observations = [
        {
            "source": "collar-xy123",
            "type": "tracking-device",
            "subject_type": "puma",
            "recorded_at": "2024-01-24 09:03:00-0300",
            "location": {
                "lat": -51.748,
                "lon": -72.720
            },
            "additional": {
                "speed_kmph": 10
            }
        }
    ]
    
    # Send the extracted data to Gundi
    await send_observations_to_gundi(observations=observations, integration_id=integration.id)

    # The result will be recorded in the portal if using the activity_logger decorator
    return {"observations_extracted": 10}
```


## Webhooks Usage:
This framework provides a way to handle incoming webhooks from external services. You can define a handler function in `webhooks/handlers.py` and define the expected payload schema and configurations in `webhooks/configurations.py`. Several base classes are provided in `webhooks/core.py` to help you define the expected schema and configurations.


### Fixed Payload Schema
If you expect to receive data with a fixed schema, you can define a Pydantic model for the payload and configurations. These models will be used for validating and parsing the incoming data.
```python
# webhooks/configurations.py
import pydantic
from .core import WebhookPayload, WebhookConfiguration


class MyWebhookPayload(WebhookPayload):
    device_id: str
    timestamp: str
    lat: float
    lon: float
    speed_kmph: float


class MyWebhookConfig(WebhookConfiguration):
    custom_setting: str
    another_custom_setting: bool

```
### Webhook Handler
Your webhook handler function must be named webhook_handler and it must accept the payload and config as arguments. The payload will be validated and parsed using the annotated Pydantic model. The config will be validated and parsed using the annotated Pydantic model. You can then implement your business logic to extract the data and send it to Gundi.
```python
# webhooks/handlers.py
from app.services.activity_logger import webhook_activity_logger
from app.services.gundi import send_observations_to_gundi
from .configurations import MyWebhookPayload, MyWebhookConfig


@webhook_activity_logger()
async def webhook_handler(payload: MyWebhookPayload, integration=None, webhook_config: MyWebhookConfig = None):
    # Implement your custom logic to process the payload here...
    
    # If the request is related to an integration, you can use the integration object to access the integration's data
    
    # Normalize the extracted data into a list of observations following to the Gundi schema:
    transformed_data = [
        {
            "source": payload.device_id,
            "type": "tracking-device",
            "recorded_at": payload.timestamp,
            "location": {
                "lat": payload.lat,
                "lon": payload.lon
            },
            "additional": {
                "speed_kmph": payload.speed_kmph
            }
        }
    ]
    await send_observations_to_gundi(
          observations=transformed_data,
          integration_id=integration.id
      )
    
    return {"observations_extracted": 1}
```

### Dynamic Payload Schema
If you expect to receive data with different schemas, you can define a schema per integration using JSON schema. To do that, annotate the payload arg with the `GenericJsonPayload` model, and annotate the webhook_config arg with the `DynamicSchemaConfig` model or a subclass. Then you can define the schema in the Gundi portal, and the framework will build the Pydantic model on runtime based on that schema, to validate and parse the incoming data.
```python
# webhooks/configurations.py
import pydantic
from .core import DynamicSchemaConfig


class MyWebhookConfig(DynamicSchemaConfig):
    custom_setting: str
    another_custom_setting: bool

```
```python
# webhooks/handlers.py
from app.services.activity_logger import webhook_activity_logger
from .core import GenericJsonPayload
from .configurations import MyWebhookConfig


@webhook_activity_logger()
async def webhook_handler(payload: GenericJsonPayload, integration=None, webhook_config: MyWebhookConfig = None):
    # Implement your custom logic to process the payload here...
    return {"observations_extracted": 1}
```


### Simple JSON Transformations
For simple JSON to JSON transformations, you can use the [JQ language](https://jqlang.github.io/jq/manual/#basic-filters) to transform the incoming data. To do that, annotate the webhook_config arg with the `GenericJsonTransformConfig` model or a subclass. Then you can specify the `jq_filter` and the `output_type` (`ev` for event or `obv` for observation) in Gundi.
```python
# webhooks/configurations.py
import pydantic
from .core import WebhookPayload, GenericJsonTransformConfig


class MyWebhookPayload(WebhookPayload):
    device_id: str
    timestamp: str
    lat: float
    lon: float
    speed_kmph: float


class MyWebhookConfig(GenericJsonTransformConfig):
    custom_setting: str
    another_custom_setting: bool


```
```python
# webhooks/handlers.py
import json
import pyjq
from app.services.activity_logger import webhook_activity_logger
from app.services.gundi import send_observations_to_gundi
from .configurations import MyWebhookPayload, MyWebhookConfig


@webhook_activity_logger()
async def webhook_handler(payload: MyWebhookPayload, integration=None, webhook_config: MyWebhookConfig = None):
    # Sample implementation using the JQ language to transform the incoming data
    input_data = json.loads(payload.json())
    transformation_rules = webhook_config.jq_filter
    transformed_data = pyjq.all(transformation_rules, input_data)
    print(f"Transformed Data:\n: {transformed_data}")
    # webhook_config.output_type == "obv":
    response = await send_observations_to_gundi(
        observations=transformed_data,
        integration_id=integration.id
    )
    data_points_qty = len(transformed_data) if isinstance(transformed_data, list) else 1
    print(f"{data_points_qty} data point(s) sent to Gundi.")
    return {"data_points_qty": data_points_qty}
```


### Dynamic Payload Schema with JSON Transformations
You can combine the dynamic schema and JSON transformations by annotating the payload arg with the `GenericJsonPayload` model, and annotating the webhook_config arg with the `GenericJsonTransformConfig` models or their subclasses. Then you can define the schema and the JQ filter in the Gundi portal, and the framework will build the Pydantic model on runtime based on that schema, to validate and parse the incoming data, and apply a [JQ filter](https://jqlang.github.io/jq/manual/#basic-filters) to transform the data.
```python
# webhooks/handlers.py
import json
import pyjq
from app.services.activity_logger import webhook_activity_logger
from app.services.gundi import send_observations_to_gundi
from .core import GenericJsonPayload, GenericJsonTransformConfig


@webhook_activity_logger()
async def webhook_handler(payload: GenericJsonPayload, integration=None, webhook_config: GenericJsonTransformConfig = None):
    # Sample implementation using the JQ language to transform the incoming data
    input_data = json.loads(payload.json())
    filter_expression = webhook_config.jq_filter.replace("\n", ""). replace(" ", "")
    transformed_data = pyjq.all(filter_expression, input_data)
    print(f"Transformed Data:\n: {transformed_data}")
    # webhook_config.output_type == "obv":
    response = await send_observations_to_gundi(
        observations=transformed_data,
        integration_id=integration.id
    )
    data_points_qty = len(transformed_data) if isinstance(transformed_data, list) else 1
    print(f"{data_points_qty} data point(s) sent to Gundi.")
    return {"data_points_qty": data_points_qty}
```


### Hex string payloads
If you expect to receive payloads containing binary data encoded as hex strings (e.g. ), you can use StructHexString, HexStringPayload and HexStringConfig which facilitate validation and parsing of hex strings. The user will define the name of the field containing the hex string and will define the structure of the data in the hex string, using Gundi.
The fields are defined in the hex_format attribute of the configuration, following the [struct module format string syntax](https://docs.python.org/3/library/struct.html#format-strings). The fields will be extracted from the hex string and made available as sub-fields in the data field of the payload. THey will be extracted in the order they are defined in the hex_format attribute.
```python
# webhooks/configurations.py
from app.services.utils import StructHexString
from .core import HexStringConfig, WebhookConfiguration


# Expected data: {"device": "BF170A","data": "6881631900003c20020000c3", "time": "1638201313", "type": "bove"}
class MyWebhookPayload(HexStringPayload, WebhookPayload):
    device: str
    time: str
    type: str
    data: StructHexString

    
class MyWebhookConfig(HexStringConfig, WebhookConfiguration):
    custom_setting: str
    another_custom_setting: bool

"""
Sample configuration in Gundi:
{
    "hex_data_field": "data",
    "hex_format": {
        "byte_order": ">",
        "fields": [
            {
                "name": "start_bit",
                "format": "B",
                "output_type": "int"
            },
            {
                "name": "v",
                "format": "I"
            },
            {
                "name": "interval",
                "format": "H",
                "output_type": "int"
            },
            {
                "name": "meter_state_1",
                "format": "B"
            },
            {
                "name": "meter_state_2",
                "format": "B",
                "bit_fields": [
                    {
                        "name": "meter_batter_alarm",
                        "end_bit": 0,
                        "start_bit": 0,
                        "output_type": "bool"
                    },
                    {
                        "name": "empty_pipe_alarm",
                        "end_bit": 1,
                        "start_bit": 1,
                        "output_type": "bool"
                    },
                    {
                        "name": "reverse_flow_alarm",
                        "end_bit": 2,
                        "start_bit": 2,
                        "output_type": "bool"
                    },
                    {
                        "name": "over_range_alarm",
                        "end_bit": 3,
                        "start_bit": 3,
                        "output_type": "bool"
                    },
                    {
                        "name": "temp_alarm",
                        "end_bit": 4,
                        "start_bit": 4,
                        "output_type": "bool"
                    },
                    {
                        "name": "ee_error",
                        "end_bit": 5,
                        "start_bit": 5,
                        "output_type": "bool"
                    },
                    {
                        "name": "transduce_in_error",
                        "end_bit": 6,
                        "start_bit": 6,
                        "output_type": "bool"
                    },
                    {
                        "name": "transduce_out_error",
                        "end_bit": 7,
                        "start_bit": 7,
                        "output_type": "bool"
                    },
                    {
                        "name": "transduce_out_error",
                        "end_bit": 7,
                        "start_bit": 7,
                        "output_type": "bool"
                    }
                ]
            },
            {
                "name": "r1",
                "format": "B",
                "output_type": "int"
            },
            {
                "name": "r2",
                "format": "B",
                "output_type": "int"
            },
            {
                "name": "crc",
                "format": "B"
            }
        ]
    }
}
"""
# The data extracted from the hex string will be made available as new sub-fields as follows:
"""
{
    "device": "AB1234",
    "time": "1638201313",
    "type": "bove",
    "data": {
        "value": "6881631900003c20020000c3",
        "format_spec": ">BIHBBBBB",
        "unpacked_data": {
            "start_bit": 104,
            "v": 1663873,
            "interval": 15360,
            "meter_state_1": 32,
            "meter_state_2": 2,
            "r1": 0,
            "r2": 0,
            "crc": 195,
            "meter_batter_alarm": True,
            "empty_pipe_alarm": True,
            "reverse_flow_alarm": False,
            "over_range_alarm": False,
            "temp_alarm": False,
            "ee_error": False,
            "transduce_in_error": False,
            "transduce_out_error": False
        }
    }
}
"""
```
Notice: This can also be combined with Dynamic Schema and JSON Transformations. In that case the hex string will be parsed first, adn then the JQ filter can be applied to the extracted data.

### Custom UI for configurations (ui schema)
It's possible to customize how the forms for configurations are displayed in the Gundi portal. 
To do that, use `FieldWithUIOptions` in your models. The `UIOptions` and `GlobalUISchemaOptions` will allow you to customize the appearance of the fields in the portal by setting any of the ["ui schema"](https://rjsf-team.github.io/react-jsonschema-form/docs/api-reference/uiSchema) supported options.

```python
# Example
import pydantic
from app.services.utils import FieldWithUIOptions, GlobalUISchemaOptions, UIOptions
from .core import AuthActionConfiguration, PullActionConfiguration


class AuthenticateConfig(AuthActionConfiguration):
    email: str  # This will be rendered with default widget and settings
    password: pydantic.SecretStr = FieldWithUIOptions(
        ...,
        format="password",
        title="Password",
        description="Password for the Global Forest Watch account.",
        ui_options=UIOptions(
            widget="password",  # This will be rendered as a password input hiding the input
        )
    )
    ui_global_options = GlobalUISchemaOptions(
        order=["email", "password"],  # This will set the order of the fields in the form
    )


class MyPullActionConfiguration(PullActionConfiguration):
    lookback_days: int = FieldWithUIOptions(
        10,
        le=30,
        ge=1,
        title="Data lookback days",
        description="Number of days to look back for data.",
        ui_options=UIOptions(
            widget="range",  # This will be rendered ad a range slider
        )
    )
    force_fetch: bool = FieldWithUIOptions(
        False,
        title="Force fetch",
        description="Force fetch even if in a quiet period.",
        ui_options=UIOptions(
            widget="radio", # This will be rendered as a radio button
        )
    )
    ui_global_options = GlobalUISchemaOptions(
        order=[
            "lookback_days",
            "force_fetch",
        ],
    )
```
