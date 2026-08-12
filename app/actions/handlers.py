import logging

from app.actions.configurations import ListDatasetsQuery, ListDatasetFieldsQuery
from app.actions.core import ReferenceDataResponse, ReferenceOption
from app.actions.datasets import DATASET_REGISTRY
from app.actions.gnwclient import DataAPI

logger = logging.getLogger(__name__)


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
