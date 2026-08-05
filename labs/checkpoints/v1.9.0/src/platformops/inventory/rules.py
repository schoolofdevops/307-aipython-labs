"""The three rule checks a platform team runs on the inventory by hand today."""

from typing import Any

from .data import MIN_PROD_MEMORY_GB

Server = dict[str, Any]


def _servers_in_environment(inventory: list[Server], env: str) -> list[Server]:
    """Return only the servers whose `env` matches the one given.

    Private helper (leading underscore -- not part of this package's public
    surface, and not re-exported from `__init__.py`). `find_low_memory_prod`
    below and `filter_by_environment` in `summary.py` both used to repeat
    this exact loop; it now lives in one place. It lives here, in `rules.py`,
    rather than in `summary.py`, so that `summary.py` can import it without
    `rules.py` ever needing to import anything back from `summary.py` --
    that two-way import is what causes a circular import (see the Deep Dive).
    """
    return [server for server in inventory if server["env"] == env]


def find_missing_owner(inventory: list[Server]) -> list[str]:
    """Return the names of servers that have no `owner` tag."""
    missing = []
    for server in inventory:
        if "owner" not in server["tags"]:
            missing.append(server["name"])
    return missing


def find_low_memory_prod(
    inventory: list[Server], min_memory_gb: int = MIN_PROD_MEMORY_GB
) -> list[str]:
    """Return the names of prod servers with less than `min_memory_gb` memory."""
    return [
        server["name"]
        for server in _servers_in_environment(inventory, "prod")
        if server["memory_gb"] < min_memory_gb
    ]


def count_by_environment(inventory: list[Server]) -> dict[str, int]:
    """Return a dict mapping each environment name to its server count."""
    counts: dict[str, int] = {}
    for server in inventory:
        env = server["env"]
        counts[env] = counts.get(env, 0) + 1
    return counts
