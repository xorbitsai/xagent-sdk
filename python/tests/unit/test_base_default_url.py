"""Base-client default base-url resolution.

`_BaseClient` resolves a base URL from, in order: the explicit argument,
``XAGENT_BASE_URL``, then the class-level ``_DEFAULT_BASE_URL``. A
subclass with no default must still require a base URL; one with a
default uses it only when nothing else is supplied. An explicitly empty
value never resolves to the default or the environment.
"""

import httpx
import pytest

from xagent_sdk._base import _BaseClient


def _ok(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={})


class _NoDefault(_BaseClient):
    pass


class _WithDefault(_BaseClient):
    _DEFAULT_BASE_URL = "https://hosted.example"


def _bearer(c: _BaseClient) -> str:
    return c._http._client.headers["Authorization"]


class TestNoDefault:
    def test_missing_base_url_raises(self) -> None:
        with pytest.raises(ValueError, match="base_url"):
            _NoDefault(api_key="k")

    def test_explicit_base_url_used(self) -> None:
        c = _NoDefault(
            api_key="k", base_url="https://x", transport=httpx.MockTransport(_ok)
        )
        assert str(c._http._client.base_url) == "https://x"
        c.close()


class TestWithDefault:
    def test_default_used_when_nothing_supplied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XAGENT_BASE_URL", raising=False)
        c = _WithDefault(api_key="k", transport=httpx.MockTransport(_ok))
        assert str(c._http._client.base_url) == "https://hosted.example"
        c.close()

    def test_explicit_overrides_default(self) -> None:
        c = _WithDefault(
            api_key="k", base_url="https://override", transport=httpx.MockTransport(_ok)
        )
        assert str(c._http._client.base_url) == "https://override"
        c.close()

    def test_env_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XAGENT_BASE_URL", "https://from-env")
        c = _WithDefault(api_key="k", transport=httpx.MockTransport(_ok))
        assert str(c._http._client.base_url) == "https://from-env"
        c.close()

    def test_explicit_empty_base_url_does_not_resolve_to_default(self) -> None:
        # An explicit "" is a caller bug; it must raise, not silently use
        # the class default.
        with pytest.raises(ValueError, match="base_url"):
            _WithDefault(api_key="k", base_url="")

    def test_empty_env_base_url_does_not_resolve_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A broken XAGENT_BASE_URL="" must fail fast, not silently fall
        # back to the hosted default (which could send staging traffic to
        # production).
        monkeypatch.setenv("XAGENT_BASE_URL", "")
        with pytest.raises(ValueError, match="base_url"):
            _WithDefault(api_key="k")

    def test_empty_env_api_key_does_not_resolve_to_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A present-but-empty key env var is a broken config -> fail fast.
        monkeypatch.setenv("XAGENT_API_KEY", "")
        with pytest.raises(ValueError, match="api_key"):
            _NoDefault(base_url="https://x")
