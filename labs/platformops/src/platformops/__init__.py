"""PlatformOps Toolkit — a Python operational toolkit for DevOps, platform
engineering and SRE work. This module grows release by release through the
course, from this v0.0 foundation to the v3.0 capstone.
"""

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
