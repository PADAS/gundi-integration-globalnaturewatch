from app.actions.datasets import DATASET_REGISTRY, QueryMode
from app.actions.tests.fields_fixtures import VIIRS_FIELDS, INTEGRATED_FIELDS


def test_registry_ships_parity_datasets():
    assert set(DATASET_REGISTRY) == {"nasa_viirs_fire_alerts", "gfw_integrated_alerts"}
    viirs = DATASET_REGISTRY["nasa_viirs_fire_alerts"]
    assert viirs.query_mode == QueryMode.SYNC
    assert viirs.date_field == "alert__date"
    integrated = DATASET_REGISTRY["gfw_integrated_alerts"]
    assert integrated.query_mode == QueryMode.BATCH
    assert integrated.date_field == "gfw_integrated_alerts__date"


def test_spec_defaults_exist_in_fields_fixtures():
    """Every default_field, date_field, lat/lon field must exist in the real
    /fields inventory committed as a fixture — catches spec/API drift."""
    fixtures = {"nasa_viirs_fire_alerts": VIIRS_FIELDS, "gfw_integrated_alerts": INTEGRATED_FIELDS}
    for key, spec in DATASET_REGISTRY.items():
        names = {f["name"] for f in fixtures[key]}
        for field in [spec.date_field, spec.lat_field, spec.lon_field, *spec.default_fields]:
            assert field in names, f"{key}: '{field}' not in /fields fixture"
