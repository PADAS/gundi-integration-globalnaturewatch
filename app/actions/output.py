"""Output strategies: rows from a dataset query -> Gundi Event dicts.

Adding a strategy = one class here + one config model in the DatasetEntry
output union + one OUTPUT_STRATEGIES entry. Handlers stay untouched.
"""
import hashlib
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean
from typing import List, Protocol

import h3

from app.actions.configurations import DatasetEntry
from app.actions.datasets import DatasetSpec

logger = logging.getLogger(__name__)


def row_fingerprint(row: dict) -> str:
    """Stable identity of a fetched record for the posted-record ledger.

    Hashes ALL values, so an upstream revision of a record (e.g. an
    integrated-alerts confidence upgrade) gets a new fingerprint and is
    posted again as an updated event — deliberate.
    """
    return hashlib.sha256(
        json.dumps(row, sort_keys=True, default=str).encode()
    ).hexdigest()[:24]


def _recorded_at(value) -> str:
    """Dataset date fields arrive as 'YYYY-MM-DD' (sometimes with a time part)."""
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value[:19])
    else:
        parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


class OutputStrategy(Protocol):
    def to_events(self, rows: List[dict], entry: DatasetEntry, spec: DatasetSpec) -> List[dict]:
        ...


class PerRecordStrategy:
    def to_events(self, rows, entry, spec):
        event_type = entry.resolved_event_type(spec)
        events = []
        for row in rows:
            details = {k: v for k, v in row.items() if k not in (spec.lat_field, spec.lon_field)}
            events.append(dict(
                title=spec.title,
                event_type=event_type,
                recorded_at=_recorded_at(row[spec.date_field]),
                location={"lat": row[spec.lat_field], "lon": row[spec.lon_field]},
                event_details=details,
            ))
        return events


class H3GridStrategy:
    def to_events(self, rows, entry, spec):
        event_type = entry.resolved_event_type(spec)
        resolution = entry.output.resolution
        cells = defaultdict(list)
        for row in rows:
            cell = h3.latlng_to_cell(row[spec.lat_field], row[spec.lon_field], resolution)
            cells[cell].append(row)

        events = []
        for cell, cell_rows in cells.items():
            lat, lon = h3.cell_to_latlng(cell)
            details = {
                "record_count": len(cell_rows),
                "cell_id": cell,
                "resolution": resolution,
            }
            numeric_fields = {
                k for row in cell_rows for k, v in row.items()
                if k not in (spec.lat_field, spec.lon_field)
                and isinstance(v, (int, float)) and not isinstance(v, bool)
            }
            for field in sorted(numeric_fields):
                values = [r[field] for r in cell_rows
                          if isinstance(r.get(field), (int, float)) and not isinstance(r.get(field), bool)]
                details[f"{field}_min"] = min(values)
                details[f"{field}_max"] = max(values)
                details[f"{field}_mean"] = mean(values)
            newest = max(r[spec.date_field] for r in cell_rows)
            events.append(dict(
                title=f"{spec.title} — {len(cell_rows)} records",
                event_type=event_type,
                recorded_at=_recorded_at(newest),
                location={"lat": lat, "lon": lon},
                event_details=details,
            ))
        return events


OUTPUT_STRATEGIES = {
    "per_record": PerRecordStrategy(),
    "h3_grid": H3GridStrategy(),
}
