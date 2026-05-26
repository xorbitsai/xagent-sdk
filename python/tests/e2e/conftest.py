"""Fixtures for end-to-end tests.

These tests require a running xAgent backend reachable at the URL in
``XAGENT_BASE_URL`` with a valid ``XAGENT_API_KEY``. Run with::

    XAGENT_BASE_URL=http://localhost:8000 XAGENT_API_KEY=xag_... \\
        uv run pytest -m e2e

Without those env vars set, every test in this directory is skipped.
CI does not run e2e tests; they are local-only.
"""

import os
from collections.abc import Iterator

import pytest

from xagent_sdk import XAgentClient


@pytest.fixture
def client() -> Iterator[XAgentClient]:
    """Default e2e client with the SDK's 30s per-request HTTP timeout."""
    api_key = os.environ.get("XAGENT_API_KEY")
    base_url = os.environ.get("XAGENT_BASE_URL")
    if not (api_key and base_url):
        pytest.skip("e2e requires XAGENT_API_KEY and XAGENT_BASE_URL")
    with XAgentClient(api_key=api_key, base_url=base_url) as c:
        yield c


@pytest.fixture
def patient_client() -> Iterator[XAgentClient]:
    """Same as ``client`` but with a 60s per-request HTTP timeout.

    Used by tests that want to *observe* an operation whose latency may
    exceed the SDK default (e.g. measuring whether POST is actually
    async). With the default 30s, a synchronous backend implementation
    would surface as a generic ``XAgentTransportError: timed out``;
    this fixture lets the call complete so the test can assert on the
    measured latency itself, giving a clearer regression signal.
    """
    api_key = os.environ.get("XAGENT_API_KEY")
    base_url = os.environ.get("XAGENT_BASE_URL")
    if not (api_key and base_url):
        pytest.skip("e2e requires XAGENT_API_KEY and XAGENT_BASE_URL")
    with XAgentClient(api_key=api_key, base_url=base_url, timeout=60.0) as c:
        yield c
