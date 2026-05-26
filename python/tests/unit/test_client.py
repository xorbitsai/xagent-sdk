"""Tests for XAgentClient construction and the me() probe."""

from collections.abc import Callable

import httpx
import pytest

from xagent_sdk import InvalidAPIKey, MeResponse, XAgentClient


def _me_handler(req: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"agent_id": 7, "agent_name": "Sales", "key_prefix": "a1B2c3"},
    )


def _401_handler(req: httpx.Request) -> httpx.Response:
    return httpx.Response(
        401,
        json={"error": {"code": "invalid_api_key", "message": "bad"}},
    )


class TestConstruction:
    def test_explicit(self, make_client: Callable[..., XAgentClient]) -> None:
        with make_client(_me_handler) as c:
            assert c.me().agent_id == 7

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XAGENT_API_KEY", "envkey")
        monkeypatch.setenv("XAGENT_BASE_URL", "https://envhost")
        c = XAgentClient(transport=httpx.MockTransport(_me_handler))
        assert c.me().agent_id == 7
        c.close()

    def test_explicit_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XAGENT_API_KEY", "envkey")
        monkeypatch.setenv("XAGENT_BASE_URL", "https://envhost")
        c = XAgentClient(
            api_key="explicit",
            base_url="https://explicit",
            transport=httpx.MockTransport(_me_handler),
        )
        # Bearer header comes from the explicit api_key, not the env value.
        assert c._http._client.headers["Authorization"] == "Bearer explicit"
        c.close()

    def test_missing_api_key(self) -> None:
        with pytest.raises(ValueError, match="api_key"):
            XAgentClient(base_url="https://x")

    def test_missing_base_url(self) -> None:
        with pytest.raises(ValueError, match="base_url"):
            XAgentClient(api_key="x")


class TestMe:
    def test_returns_me_response(
        self, make_client: Callable[..., XAgentClient]
    ) -> None:
        with make_client(_me_handler) as c:
            me = c.me()
        assert isinstance(me, MeResponse)
        assert me.agent_name == "Sales"

    def test_401_raises_invalid_api_key(
        self, make_client: Callable[..., XAgentClient]
    ) -> None:
        with make_client(_401_handler) as c, pytest.raises(InvalidAPIKey):
            c.me()


class TestLifecycle:
    def test_context_manager_closes(
        self, make_client: Callable[..., XAgentClient]
    ) -> None:
        c = make_client(_me_handler)
        assert c._http._client.is_closed is False
        with c:
            pass
        assert c._http._client.is_closed is True
