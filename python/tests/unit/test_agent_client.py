"""Tests for AgentClient construction and context-manager lifecycle.

The ``me()`` method used to live here in 0.1.0 (it returned the
agent identity bound to the runtime key). 0.2.0 moved identity to
``UserClient.me()`` and ``AgentClient`` no longer has a probe method,
so this module focuses on the construction contract and resource
cleanup. Auth-mapping coverage for the agent runtime key (401 ->
``InvalidAPIKey``) lives in ``test_tasks.py`` and ``test_errors.py``.
"""

from collections.abc import Callable

import httpx
import pytest

from xagent_sdk import AgentClient


def _ok_handler(req: httpx.Request) -> httpx.Response:
    """Generic 200 OK; tests below do not actually send requests, but
    ``make_client`` requires a handler argument."""
    return httpx.Response(200, json={"ok": True})


class TestConstruction:
    def test_explicit(self, make_client: Callable[..., AgentClient]) -> None:
        with make_client(_ok_handler) as c:
            # Explicit api_key from the factory default flows to the
            # Authorization header verbatim.
            assert c._http._client.headers["Authorization"] == "Bearer test_key"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XAGENT_API_KEY", "envkey")
        monkeypatch.setenv("XAGENT_BASE_URL", "https://envhost")
        c = AgentClient(transport=httpx.MockTransport(_ok_handler))
        assert c._http._client.headers["Authorization"] == "Bearer envkey"
        c.close()

    def test_explicit_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XAGENT_API_KEY", "envkey")
        monkeypatch.setenv("XAGENT_BASE_URL", "https://envhost")
        c = AgentClient(
            api_key="explicit",
            base_url="https://explicit",
            transport=httpx.MockTransport(_ok_handler),
        )
        # Bearer header comes from the explicit api_key, not the env value.
        assert c._http._client.headers["Authorization"] == "Bearer explicit"
        c.close()

    def test_missing_api_key(self) -> None:
        with pytest.raises(ValueError, match="api_key"):
            AgentClient(base_url="https://x")

    def test_missing_base_url(self) -> None:
        with pytest.raises(ValueError, match="base_url"):
            AgentClient(api_key="x")


class TestLifecycle:
    def test_context_manager_closes(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        c = make_client(_ok_handler)
        assert c._http._client.is_closed is False
        with c:
            pass
        assert c._http._client.is_closed is True
