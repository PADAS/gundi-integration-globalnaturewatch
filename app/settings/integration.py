# Add your integration-specific settings here

from environs import Env

env = Env()
env.read_env()

INTEGRATION_TYPE_NAME = env.str("INTEGRATION_TYPE_NAME", "Global Nature Watch")

# Caps concurrent requests to the GFW Data API query endpoints per instance.
# GFW_practical_ceiling(~50) >= GNW_DATASET_QUERY_CONCURRENCY * max_instances * concurrent_requests_per_instance
GNW_DATASET_QUERY_CONCURRENCY = env.int("GNW_DATASET_QUERY_CONCURRENCY", 5)


# Patch store: the gnw_patches database on the environment's pipeline Cloud SQL
# instance. Unset host = no patch store, and anything that needs it fails with
# PatchStoreNotConfigured. The password comes from Secret Manager.
GNW_PATCHES_DB_HOST = env.str("GNW_PATCHES_DB_HOST", None)
GNW_PATCHES_DB_PORT = env.int("GNW_PATCHES_DB_PORT", 5432)
GNW_PATCHES_DB_NAME = env.str("GNW_PATCHES_DB_NAME", "gnw_patches")
GNW_PATCHES_DB_USER = env.str("GNW_PATCHES_DB_USER", "gnw_patches")
GNW_PATCHES_DB_PASSWORD = env.str("GNW_PATCHES_DB_PASSWORD", None)
GNW_PATCHES_DB_CONNECT_TIMEOUT = env.int("GNW_PATCHES_DB_CONNECT_TIMEOUT", 10)
