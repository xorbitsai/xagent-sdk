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
    api_key = os.environ.get("XAGENT_API_KEY")
    base_url = os.environ.get("XAGENT_BASE_URL")
    if not (api_key and base_url):
        pytest.skip("e2e requires XAGENT_API_KEY and XAGENT_BASE_URL")
    with XAgentClient(api_key=api_key, base_url=base_url) as c:
        yield c
