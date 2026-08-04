"""Print the plain-text inventory report -- the only file that talks to the terminal."""

from .data import INVENTORY, MIN_PROD_MEMORY_GB
from .rules import count_by_environment, find_low_memory_prod, find_missing_owner
from .summary import build_summary, filter_by_environment, sort_by_name


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
