import hashlib
import json
from datetime import date
from typing import List, Literal, Optional, Union

import pydantic

from app.actions.core import (
    AuthActionConfiguration, ExecutableActionMixin, InternalActionConfiguration,
    PullActionConfiguration, ReferenceActionConfiguration,
)
from app.actions.datasets import DATASET_REGISTRY, DatasetSpec
from app.services.errors import ConfigurationNotFound
from app.services.utils import find_config_for_action


class ListDatasetsQuery(ReferenceActionConfiguration):
    pass


class ListDatasetFieldsQuery(ReferenceActionConfiguration):
    dataset: str
    filterable_only: bool = False

    @pydantic.validator("dataset")
    def dataset_in_registry(cls, v):
        if v not in DATASET_REGISTRY:
            raise ValueError(f"Unknown dataset '{v}'. Available: {sorted(DATASET_REGISTRY)}")
        return v


def _reference(action: str, params: Optional[dict] = None) -> dict:
    """Build a gundi:reference ui_schema annotation (contract vendored from
    gundi-integration-cmore). Deliberately does NOT set ui:widget — portals
    without reference support must keep rendering plain text fields."""
    return {"action": action, "target": "self", "params": params or {}, "allow_free_text": True}


class AuthenticateConfig(AuthActionConfiguration, ExecutableActionMixin):
    email: str
    password: pydantic.SecretStr = pydantic.Field(
        ..., format="password", title="Password",
        description="Password for the Global Nature Watch / Global Forest Watch account.",
    )


def get_auth_config(integration) -> "AuthenticateConfig":
    auth_config = find_config_for_action(
        configurations=integration.configurations, action_id="auth"
    )
    if not auth_config:
        raise ConfigurationNotFound(
            f"Authentication settings for integration {str(integration.id)} "
            f"are missing. Please fix the integration setup in the portal."
        )
    return AuthenticateConfig.parse_obj(auth_config.data)


class FilterRow(pydantic.BaseModel):
    field: str = pydantic.Field(..., title="Field")
    operator: Literal["=", "!=", ">", ">=", "<", "<=", "in"] = pydantic.Field("=", title="Operator")
    value: str = pydantic.Field(
        ..., title="Value",
        description="Parsed to the field's data type. For 'in', a comma-separated list.",
    )


class PerRecordOutput(pydantic.BaseModel):
    mode: Literal["per_record"] = "per_record"


class H3GridOutput(pydantic.BaseModel):
    mode: Literal["h3_grid"] = "h3_grid"
    resolution: int = pydantic.Field(
        7, ge=4, le=10, title="H3 Resolution",
        description="H3 cell resolution: 6 is ~36 km2 hexes, 7 is ~5 km2, 8 is ~0.7 km2.",
    )


class DatasetEntry(pydantic.BaseModel):
    dataset: str = pydantic.Field(..., title="Dataset")
    fields: List[str] = pydantic.Field(
        default_factory=list, title="Extra Fields",
        description="Fields to include in event details, beyond the dataset defaults.",
    )
    filters: List[FilterRow] = pydantic.Field(default_factory=list, title="Filters (all must match)")
    output: Union[PerRecordOutput, H3GridOutput] = pydantic.Field(
        default_factory=PerRecordOutput, discriminator="mode", title="Output Mode",
    )
    event_type: Optional[str] = pydantic.Field(
        None, title="Event Type",
        description="EarthRanger event type. Leave blank to use the dataset default.",
    )
    lookback_days: Optional[int] = pydantic.Field(None, ge=1, le=365, title="Lookback Days")

    def resolved_event_type(self, spec: DatasetSpec) -> str:
        if self.event_type:
            return self.event_type
        suffix = "_agg" if self.output.mode == "h3_grid" else ""
        return f"{spec.default_event_type}{suffix}"

    def resolved_lookback_days(self, spec: DatasetSpec) -> int:
        return self.lookback_days or spec.default_lookback_days

    class Config:
        @staticmethod
        def schema_extra(schema: dict, model) -> None:
            # pydantic v1 emits the OpenAPI-only "discriminator" keyword for
            # the output union. The portal's react-jsonschema-form runs ajv in
            # strict mode, which refuses to compile schemas with unknown
            # keywords ("strict mode: unknown keyword: discriminator"), so the
            # config form cannot validate or save. Strip it from the emitted
            # schema; runtime parsing still discriminates via the Field.
            schema.get("properties", {}).get("output", {}).pop("discriminator", None)


class PullEventsConfig(PullActionConfiguration):
    aoi_url: pydantic.HttpUrl = pydantic.Field(
        ..., title="AOI Share Link",
        description="AOI share link from your Global Nature Watch / MyGFW dashboard.",
    )
    dataset_entries: List[DatasetEntry] = pydantic.Field(
        default_factory=list, title="Datasets to Pull",
    )
    force_fetch: bool = pydantic.Field(
        False, title="Force Fetch",
        description="Fetch even if the dataset reports no new data. Use sparingly.",
    )

    @classmethod
    def ui_schema(cls):
        ui = super().ui_schema()
        items = ui.setdefault("dataset_entries", {}).setdefault("items", {})
        items.setdefault("dataset", {})["gundi:reference"] = _reference("list_datasets")
        # $data paths: an array and its items count as separate levels (cmore convention)
        items.setdefault("fields", {}).setdefault("items", {})["gundi:reference"] = _reference(
            "list_dataset_fields", {"dataset": {"$data": "../../dataset"}}
        )
        filter_items = items.setdefault("filters", {}).setdefault("items", {})
        filter_items.setdefault("field", {})["gundi:reference"] = _reference(
            "list_dataset_fields",
            {"dataset": {"$data": "../../../../dataset"}, "filterable_only": True},
        )
        return ui


class RunQueryJobConfig(InternalActionConfiguration):
    entry: DatasetEntry
    geostore_ids: List[str]
    window_start: date
    window_end: date
    submit_new: bool = True  # batch mode: submit a new job this run (version gate result)


def entry_state_key(entry: DatasetEntry) -> str:
    """Stable identity for an entry's state (quiet periods, anchors, jobs).
    Insensitive to entry order in the config and to filter row order."""
    payload = json.dumps(
        {
            "dataset": entry.dataset,
            "fields": sorted(entry.fields),
            "filters": sorted([f.dict() for f in entry.filters],
                              key=lambda f: (f["field"], f["operator"], f["value"])),
            "output": entry.output.dict(),
            "event_type": entry.event_type,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
