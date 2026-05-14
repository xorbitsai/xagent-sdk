"""Tests for the HTTPClient transport wrapper."""

import httpx
import pytest

from xagent_sdk import XAgentTransportError
from xagent_sdk._http import HTTPClient
from xagent_sdk._version import __version__


def _ok_handler(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


class TestHTTPClientConstruction:
    def test_empty_api_key(self) -> None:
        with pytest.raises(ValueError, match="api_key"):
            HTTPClient(base_url="https://x", api_key="")

    def test_empty_base_url(self) -> None:
        with pytest.raises(ValueError, match="base_url"):
            HTTPClient(base_url="", api_key="x")

    def test_base_url_normalization(self) -> None:
        c = HTTPClient(
            base_url="https://host/",
            api_key="x",
            transport=httpx.MockTransport(_ok_handler),
        )
        assert str(c._client.base_url).rstrip("/") == "https://host"
        c.close()

    def test_default_headers(self) -> None:
        c = HTTPClient(
            base_url="https://x",
            api_key="xag_secret",
            transport=httpx.MockTransport(_ok_handler),
        )
        h = c._client.headers
        assert h["Authorization"] == "Bearer xag_secret"
        assert __version__ in h["User-Agent"]
        assert "httpx/" in h["User-Agent"]
        assert h["Accept"] == "application/json"
        c.close()

    def test_user_agent_override(self) -> None:
        c = HTTPClient(
            base_url="https://x",
            api_key="y",
            user_agent="my-app/1.0",
            transport=httpx.MockTransport(_ok_handler),
        )
        assert c._client.headers["User-Agent"] == "my-app/1.0"
        c.close()


class TestHTTPClientRequest:
    def test_returns_raw_response(self) -> None:
        c = HTTPClient(
            base_url="https://x",
            api_key="y",
            transport=httpx.MockTransport(_ok_handler),
        )
        resp = c.request("GET", "/v1/me")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        c.close()

    def test_does_not_raise_on_4xx(self) -> None:
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {}})

        c = HTTPClient(
            base_url="https://x",
            api_key="y",
            transport=httpx.MockTransport(h),
        )
        # Raw 4xx flows through; error mapping is the caller's job.
        resp = c.request("GET", "/v1/me")
        assert resp.status_code == 401
        c.close()

    def test_wraps_transport_error(self) -> None:
        def boom(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dns dead")

        c = HTTPClient(
            base_url="https://x",
            api_key="y",
            transport=httpx.MockTransport(boom),
        )
        with pytest.raises(XAgentTransportError) as excinfo:
            c.request("GET", "/v1/me")
        assert excinfo.value.code == "transport_error"
        assert "dns dead" in excinfo.value.message
        assert isinstance(excinfo.value.__cause__, httpx.ConnectError)
        c.close()


class TestHTTPClientLifecycle:
    def test_close(self) -> None:
        c = HTTPClient(
            base_url="https://x",
            api_key="y",
            transport=httpx.MockTransport(_ok_handler),
        )
        assert c._client.is_closed is False
        c.close()
        assert c._client.is_closed is True

    def test_context_manager(self) -> None:
        with HTTPClient(
            base_url="https://x",
            api_key="y",
            transport=httpx.MockTransport(_ok_handler),
        ) as c:
            assert c._client.is_closed is False
        assert c._client.is_closed is True
