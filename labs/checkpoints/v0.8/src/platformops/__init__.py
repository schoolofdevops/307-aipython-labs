"""PlatformOps Toolkit — a Python operational toolkit for DevOps, platform
engineering and SRE work. This module grows release by release through the
course, from this v0.0 foundation to the v3.0 capstone.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version

try:
    # Read the version from the package's own install metadata -- the same
    # metadata `uv sync` writes from pyproject.toml's `version =` line. One
    # source of truth: bump pyproject.toml, run `uv sync`, and every place
    # that reports a version (this constant, `platformops version`) picks
    # up the new number without a second place to edit.
    __version__ = _installed_version("platformops")
except (
    PackageNotFoundError
):  # pragma: no cover -- running from a bare checkout, never installed
    __version__ = "0.0.0"


def about() -> str:
    """Return the toolkit's one-line tagline.

    This is the smallest possible piece of the toolkit: a function you can
    import, call and test. Every later module adds real capability next to
    it — the shape (a tested function in this package) does not change.
    """
    return f"PlatformOps Toolkit v{__version__} — inspect, validate and troubleshoot your services"


def main() -> None:
    print(about())
