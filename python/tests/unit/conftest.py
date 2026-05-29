"""Shared fixtures for unit tests.

All unit tests use ``httpx.MockTransport`` via the ``make_client``
fixture; no test reaches the network. The ``clean_xagent_env`` autouse
fixture guards against env-var contamination across tests.
"""

from collections.abc import Callable

import httpx
import pytest

from xagent_sdk import AgentClient


@pytest.fixture(autouse=True)
def clean_xagent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip XAGENT_* env vars so tests do not inherit ambient config."""
    monkeypatch.delenv("XAGENT_API_KEY", raising=False)
    monkeypatch.delenv("XAGENT_PERSONAL_KEY", raising=False)
    monkeypatch.delenv("XAGENT_BASE_URL", raising=False)


@pytest.fixture
def make_client() -> Callable[..., AgentClient]:
    """Return a factory for AgentClient backed by an httpx.MockTransport."""

    def _factory(
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        api_key: str = "test_key",
        base_url: str = "https://test.example",
    ) -> AgentClient:
        return AgentClient(
            api_key=api_key,
            base_url=base_url,
            transport=httpx.MockTransport(handler),
        )

    return _factory
