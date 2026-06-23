"""Tests for WorkspaceClient construction and credential resolution."""

import httpx
import pytest

from xagent_sdk.cloud import Region, WorkspaceClient


def _ok(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={})


def _bearer(c: WorkspaceClient) -> str:
    return c._http._client.headers["Authorization"]


class TestConstruction:
    def test_explicit_key_and_url(self) -> None:
        c = WorkspaceClient(
            workspace_key="xag_workspace_p_s",
            base_url="https://test.example",
            transport=httpx.MockTransport(_ok),
        )
        assert _bearer(c) == "Bearer xag_workspace_p_s"
        assert str(c._http._client.base_url) == "https://test.example"
        c.close()

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XAGENT_WORKSPACE_KEY", "xag_workspace_env_sec")
        monkeypatch.setenv("XAGENT_BASE_URL", "https://envhost")
        c = WorkspaceClient(transport=httpx.MockTransport(_ok))
        assert _bearer(c) == "Bearer xag_workspace_env_sec"
        assert str(c._http._client.base_url) == "https://envhost"
        c.close()

    def test_env_isolation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # WorkspaceClient must read only XAGENT_WORKSPACE_KEY, never the
        # runtime/personal key vars.
        monkeypatch.setenv("XAGENT_API_KEY", "runtime-not-workspace")
        monkeypatch.setenv("XAGENT_PERSONAL_KEY", "personal-not-workspace")
        monkeypatch.setenv("XAGENT_BASE_URL", "https://envhost")
        with pytest.raises(ValueError, match="workspace_key"):
            WorkspaceClient()

    def test_region_resolves_to_url(self) -> None:
        c = WorkspaceClient(
            workspace_key="xag_workspace_p_s",
            region=Region.SG,
            transport=httpx.MockTransport(_ok),
        )
        assert str(c._http._client.base_url) == "https://sg.cloud.xagent.co"
        c.close()
        c = WorkspaceClient(
            workspace_key="xag_workspace_p_s",
            region=Region.AU,
            transport=httpx.MockTransport(_ok),
        )
        assert str(c._http._client.base_url) == "https://au.cloud.xagent.co"
        c.close()

    def test_region_and_base_url_conflict(self) -> None:
        with pytest.raises(ValueError, match="region or base_url"):
            WorkspaceClient(
                workspace_key="xag_workspace_p_s",
                region=Region.SG,
                base_url="https://x",
            )

    def test_no_region_no_base_url_no_env_raises(self) -> None:
        # No region, no base_url, no XAGENT_BASE_URL -> fail fast. There is
        # no hosted default to guess (the service is per-region).
        with pytest.raises(ValueError, match="base_url"):
            WorkspaceClient(workspace_key="xag_workspace_p_s")

    def test_empty_key_no_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An explicit empty key must raise, never resolve to the env value.
        monkeypatch.setenv("XAGENT_WORKSPACE_KEY", "xag_workspace_env_sec")
        with pytest.raises(ValueError, match="workspace_key"):
            WorkspaceClient(workspace_key="")

    def test_missing_key(self) -> None:
        with pytest.raises(ValueError, match="workspace_key"):
            WorkspaceClient(base_url="https://x")

    def test_empty_env_base_url_fails_fast(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A broken XAGENT_BASE_URL="" must fail fast, not be swallowed.
        monkeypatch.setenv("XAGENT_BASE_URL", "")
        with pytest.raises(ValueError, match="base_url"):
            WorkspaceClient(workspace_key="xag_workspace_p_s")
