"""Lets `uv run python -m platformops.inventory` keep working now that
`inventory` is a package instead of a single file.

A plain module (`inventory.py`) runs itself when you name it with `-m`
because there is only one file to run. A package has no single file to
run -- Python needs to be told which file inside the package is the entry
point when it is invoked with `-m`. That file must be named exactly
`__main__.py`.
"""

from .report import print_report

if __name__ == "__main__":
    print_report()
