"""Filter, sort and summarize the inventory -- the Phase B extension from M3."""

from .rules import _servers_in_environment, count_by_environment


def filter_by_environment(inventory, env):
    """Return only the servers whose `env` matches the one given."""
    return _servers_in_environment(inventory, env)


def sort_by_name(inventory):
    """Return a new list of servers sorted by name.

    Uses `sorted()`, not `.sort()`, on purpose: `.sort()` would rearrange the
    original list in place, so any other part of the program holding onto
    that same list would see it reordered too. `sorted()` builds and
    returns a brand new list, leaving the original untouched.
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
