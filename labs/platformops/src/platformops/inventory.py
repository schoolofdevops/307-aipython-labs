"""Infrastructure inventory reporter — PlatformOps v0.1.

A small, in-memory list of servers, and a few checks a platform team runs
on it by hand today: who forgot to tag a server with an owner, which prod
servers are under-provisioned on memory, and how many servers sit in each
environment.
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


def find_missing_owner(inventory):
    """Return the names of servers that have no `owner` tag."""
    missing = []
    for server in inventory:
        if "owner" not in server["tags"]:
            missing.append(server["name"])
    return missing


def find_low_memory_prod(inventory, min_memory_gb=MIN_PROD_MEMORY_GB):
    """Return the names of prod servers with less than `min_memory_gb` memory."""
    flagged = []
    for server in inventory:
        if server["env"] == "prod" and server["memory_gb"] < min_memory_gb:
            flagged.append(server["name"])
    return flagged


def count_by_environment(inventory):
    """Return a dict mapping each environment name to its server count."""
    counts = {}
    for server in inventory:
        env = server["env"]
        counts[env] = counts.get(env, 0) + 1
    return counts


def filter_by_environment(inventory, env):
    """Return only the servers whose `env` matches the one given."""
    return [server for server in inventory if server["env"] == env]


def sort_by_name(inventory):
    """Return a new list of servers sorted by name.

    Uses `sorted()`, not `.sort()`, on purpose: `.sort()` would rearrange the
    original `INVENTORY` list in place, so any other part of the program
    holding onto that same list would see it reordered too. `sorted()`
    builds and returns a brand new list, leaving the original untouched.
    """
    return sorted(inventory, key=lambda server: server["name"])


def build_summary(inventory):
    """Return counts and totals across the whole inventory."""
    return {
        "total_servers": len(inventory),
        "total_cpu": sum(server["cpu"] for server in inventory),
        "total_memory_gb": sum(server["memory_gb"] for server in inventory),
        "by_environment": count_by_environment(inventory),
    }


def print_report():
    """Print a plain-text infrastructure inventory report."""
    print("PlatformOps Inventory Report")
    print("=============================")
    print(f"Total servers: {len(INVENTORY)}")

    print("\nServers by environment:")
    for env, count in count_by_environment(INVENTORY).items():
        print(f"  {env}: {count}")

    print("\nMissing owner tag:")
    missing = find_missing_owner(INVENTORY)
    if missing:
        for name in missing:
            print(f"  - {name}")
    else:
        print("  (none)")

    print(f"\nProd servers with low memory (< {MIN_PROD_MEMORY_GB}GB):")
    low_memory = find_low_memory_prod(INVENTORY)
    if low_memory:
        for name in low_memory:
            print(f"  - {name}")
    else:
        print("  (none)")

    print("\nSummary:")
    summary = build_summary(INVENTORY)
    print(f"  Total CPU (cores): {summary['total_cpu']}")
    print(f"  Total memory (GB): {summary['total_memory_gb']}")
    for env, count in summary["by_environment"].items():
        print(f"  {env}: {count} server(s)")

    print("\nProd servers (filtered):")
    for server in filter_by_environment(INVENTORY, "prod"):
        print(f"  - {server['name']}")

    print("\nAll servers, sorted by name:")
    for server in sort_by_name(INVENTORY):
        print(f"  - {server['name']} ({server['env']})")


if __name__ == "__main__":
    print_report()
