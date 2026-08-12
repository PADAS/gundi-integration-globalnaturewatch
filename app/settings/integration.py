# Add your integration-specific settings here

from environs import Env

env = Env()
env.read_env()

INTEGRATION_TYPE_NAME = env.str("INTEGRATION_TYPE_NAME", "Global Nature Watch")

# Caps concurrent requests to the GFW Data API query endpoints per instance.
# GFW_practical_ceiling(~50) >= GNW_DATASET_QUERY_CONCURRENCY * max_instances * concurrent_requests_per_instance
GNW_DATASET_QUERY_CONCURRENCY = env.int("GNW_DATASET_QUERY_CONCURRENCY", 5)
