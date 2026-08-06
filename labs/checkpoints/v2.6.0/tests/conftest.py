"""Shared fixtures for tests that need a real, running Floci container.

`reportstore`, `findingsstore` and `workqueue` are tested against a real
local Floci instance, not a mock -- the point of Module 22 is proving Floci
itself works as a local stand-in, not re-proving what Module 21 already
proved about mocking boto3 with moto. A learner (or CI) without Floci
running should see these test files skip cleanly, not fail with a
connection error buried in a traceback.
"""

from __future__ import annotations

import httpx
import pytest

FLOCI_HEALTH_URL = "http://localhost:4566/_floci/health"


@pytest.fixture(scope="session")
def require_floci() -> None:
    """Skip the whole test file if Floci is not reachable at localhost:4566."""
    try:
        response = httpx.get(FLOCI_HEALTH_URL, timeout=2.0)
        response.raise_for_status()
    except httpx.HTTPError:
        pytest.skip(
            "Floci is not reachable at http://localhost:4566 -- run `floci start` first"
        )
