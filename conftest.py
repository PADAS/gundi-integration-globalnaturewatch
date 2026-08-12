import pytest


@pytest.fixture(autouse=True)
def patch_integration_type_name_for_template_tests(mocker):
    """
    Patch INTEGRATION_TYPE_NAME to None for template tests.
    This allows template self-registration tests to derive type names from slugs,
    not from the integration-specific "Global Nature Watch" setting.
    Without this, template tests that mock INTEGRATION_TYPE_SLUG fail because
    the code uses the hardcoded INTEGRATION_TYPE_NAME instead of deriving from slug.
    """
    mocker.patch("app.services.self_registration.INTEGRATION_TYPE_NAME", None)
