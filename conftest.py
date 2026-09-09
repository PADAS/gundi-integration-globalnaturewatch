# Root conftest: runs before pytest imports anything under app/, so an
# environment default set here is in place before app.settings loads.
import os

# Tests share Gundi OAuth tokens within the process only. The runner's default
# is redis://<REDIS_HOST>:<REDIS_PORT>/2, and a developer with a local Redis up
# would otherwise have a token minted by one pytest run persisted and served to
# the next, while CI (no Redis) sees none of it. An explicit value in the
# environment is respected.
os.environ.setdefault("GUNDI_TOKEN_CACHE_URL", "")

import pytest  # noqa: E402  (after the env default above, on purpose)


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
