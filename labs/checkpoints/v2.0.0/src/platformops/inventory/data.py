"""Infrastructure inventory reporter — PlatformOps v0.2.

The raw inventory data lives here and nowhere else. Every other file in
this package imports `INVENTORY` from this module instead of holding its
own copy of it -- one source of truth for what a "server" record looks
like.
"""

MIN_PROD_MEMORY_GB = 8

INVENTORY = [
    {
        "name": "web-01",
        "env": "prod",
        "cpu": 4,
        "memory_gb": 16,
        "tags": {"owner": "web-team", "tier": "frontend"},
    },
    {
        "name": "web-02",
        "env": "prod",
        "cpu": 4,
        "memory_gb": 6,
        "tags": {"tier": "frontend"},
    },
    {
        "name": "api-01",
        "env": "prod",
        "cpu": 8,
        "memory_gb": 32,
        "tags": {"owner": "platform-team", "tier": "backend"},
    },
    {
        "name": "api-02",
        "env": "prod",
        "cpu": 8,
        "memory_gb": 4,
        "tags": {"owner": "platform-team", "tier": "backend"},
    },
    {
        "name": "db-01",
        "env": "prod",
        "cpu": 16,
        "memory_gb": 64,
        "tags": {"owner": "data-team", "tier": "database"},
    },
    {
        "name": "cache-01",
        "env": "staging",
        "cpu": 2,
        "memory_gb": 8,
        "tags": {"owner": "platform-team", "tier": "cache"},
    },
    {"name": "worker-01", "env": "staging", "cpu": 4, "memory_gb": 8, "tags": {}},
    {
        "name": "worker-02",
        "env": "dev",
        "cpu": 2,
        "memory_gb": 4,
        "tags": {"owner": "dev-team"},
    },
    {
        "name": "build-01",
        "env": "dev",
        "cpu": 4,
        "memory_gb": 8,
        "tags": {"owner": "ci-team", "tier": "ci"},
    },
    {
        "name": "monitor-01",
        "env": "prod",
        "cpu": 4,
        "memory_gb": 8,
        "tags": {"owner": "sre-team", "tier": "observability"},
    },
]
