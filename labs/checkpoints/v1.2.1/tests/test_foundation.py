import re

from platformops import __version__, about


def test_version_matches_the_installed_package_version():
    # This test used to read `assert __version__ == "0.0.0"` -- a value that
    # was correct on the day it was written and never checked again. Every
    # release since v0.0 bumped `pyproject.toml`'s version and left this
    # constant behind, so the test kept passing while quietly asserting a
    # lie. `platformops.__version__` now reads the real installed version
    # (M8), so this checks the *shape* of the version string instead of a
    # value that would go stale again the next time this project is tagged.
    assert re.match(r"^\d+\.\d+\.\d+$", __version__)


def test_about_returns_the_toolkit_tagline():
    message = about()
    assert "PlatformOps Toolkit" in message
    assert __version__ in message
