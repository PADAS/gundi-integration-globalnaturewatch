"""Safe SQL construction for GFW Data API dataset queries.

Identifiers are allowlisted against the dataset's live /fields inventory;
values are parsed to the field's data_type and rendered as typed literals.
No user-supplied string is ever interpolated unvalidated. String literals
use ANSI single-quote-doubling (`'` -> `''`); backslashes are rejected
outright rather than guessing the backend's escaping conventions.
"""
import datetime
import math
from typing import List, Optional

from app.actions.datasets import DatasetSpec
from app.actions.gnwclient import DatasetField

VALID_OPERATORS = {"=", "!=", ">", ">=", "<", "<=", "in"}

# data_type strings observed in the Data API's /fields responses
NUMERIC_TYPES = {"numeric", "integer", "bigint", "smallint", "double precision", "real", "float", "int"}
BOOLEAN_TYPES = {"boolean", "bool"}
DATE_TYPES = {"date", "timestamp", "timestamp without time zone"}
TEXT_TYPES = {"text", "character varying", "varchar", "string"}


class ConfigValidationError(Exception):
    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _canonical_number(value: str):
    try:
        return str(int(value))
    except ValueError:
        pass
    try:
        parsed = float(value)
    except ValueError:
        return None
    if not math.isfinite(parsed):
        return None
    return repr(parsed)


def render_literal(value: str, data_type: Optional[str]) -> str:
    value = value.strip()
    if "\\" in value:
        raise ConfigValidationError(["Backslash is not allowed in filter values"])
    if data_type in NUMERIC_TYPES:
        num = _canonical_number(value)
        if num is None:
            raise ConfigValidationError([f"'{value}' is not a valid number"])
        return num
    if data_type in BOOLEAN_TYPES:
        if value.lower() not in ("true", "false"):
            raise ConfigValidationError([f"'{value}' is not a valid boolean"])
        return value.upper()
    if data_type in DATE_TYPES:
        try:
            parsed_date = datetime.date.fromisoformat(value)
        except ValueError:
            raise ConfigValidationError([f"'{value}' is not a valid ISO date"])
        return _quote(parsed_date.isoformat())
    if data_type is None or data_type not in TEXT_TYPES:
        # Unknown type metadata (raster datasets report data_type=null):
        # values that parse as numbers render as numbers, everything else
        # is quoted and escaped. Parsed output only — never raw input.
        num = _canonical_number(value)
        if num is not None:
            return num
        return _quote(value)
    return _quote(value)


def build_query(*, spec: DatasetSpec, extra_fields: List[str], filters: List[dict],
                window_start: datetime.date, window_end: datetime.date,
                dataset_fields: List[DatasetField]) -> str:
    errors = []
    by_name = {f.name: f for f in dataset_fields}

    select_fields = [spec.lat_field, spec.lon_field, spec.date_field]
    for name in [*spec.default_fields, *extra_fields]:
        if name not in select_fields:
            select_fields.append(name)
    for name in select_fields:
        if name not in by_name:
            errors.append(f"Unknown field '{name}' for dataset '{spec.title}'")

    clauses = [f"({spec.date_field} >= '{window_start.isoformat()}'"
               f" AND {spec.date_field} < '{window_end.isoformat()}')"]
    for flt in filters:
        field, operator, value = flt["field"], flt["operator"], flt["value"]
        if operator not in VALID_OPERATORS:
            errors.append(f"Invalid operator '{operator}'")
            continue
        dataset_field = by_name.get(field)
        if dataset_field is None:
            errors.append(f"Unknown filter field '{field}'")
            continue
        if not dataset_field.is_filter:
            errors.append(f"Field '{field}' is not filterable")
            continue
        try:
            if operator == "in":
                items = [render_literal(v, dataset_field.data_type) for v in value.split(",")]
                clauses.append(f"{field} IN ({','.join(items)})")
            else:
                clauses.append(f"{field} {operator} {render_literal(value, dataset_field.data_type)}")
        except ConfigValidationError as e:
            errors.append(f"Filter on '{field}': {e.errors[0]}")

    if errors:
        raise ConfigValidationError(errors)

    return f"SELECT {','.join(select_fields)} FROM results WHERE {' AND '.join(clauses)}"
