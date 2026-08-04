"""Infrastructure inventory reporter — PlatformOps v0.2.

This used to be one file, `inventory.py`. It is now a package: a folder with
its own `__init__.py`. The public surface stays exactly the same on purpose
-- everything this package exposed as `platformops.inventory.<name>` before
the split is re-exported here, so no import anywhere else in the project
(or in your tests) has to change.
"""

from .data import INVENTORY, MIN_PROD_MEMORY_GB
from .report import print_report
from .rules import count_by_environment, find_low_memory_prod, find_missing_owner
from .summary import build_summary, filter_by_environment, sort_by_name

__all__ = [
    "INVENTORY",
    "MIN_PROD_MEMORY_GB",
    "build_summary",
    "count_by_environment",
    "filter_by_environment",
    "find_low_memory_prod",
    "find_missing_owner",
    "print_report",
    "sort_by_name",
]
