from enum import Enum
from typing import Dict, List

import pydantic


class QueryMode(str, Enum):
    SYNC = "sync"    # GET /dataset/{d}/latest/query/json, date-sliced windows
    BATCH = "batch"  # POST /dataset/{d}/latest/query/batch + job polling


class DatasetSpec(pydantic.BaseModel):
    """Curated knowledge about a dataset that the Data API can't tell us."""
    title: str
    date_field: str
    query_mode: QueryMode
    default_event_type: str
    default_fields: List[str]
    default_lookback_days: int
    lat_field: str = "latitude"
    lon_field: str = "longitude"


DATASET_REGISTRY: Dict[str, DatasetSpec] = {
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
