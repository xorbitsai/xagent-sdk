"""Tests for WorkspaceClient.templates (WorkspaceTemplatesAPI)."""

import httpx
import pytest

from xagent_sdk import Template, TemplateDetail, TemplateNotFound
from xagent_sdk.cloud import WorkspaceClient

from .._fixtures import error_envelope, response


def _make_ws(handler: object) -> WorkspaceClient:
    return WorkspaceClient(
        workspace_key="xag_workspace_p_s",
        base_url="https://test.example",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


class TestList:
    def test_url_and_parse(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("templates_list"))

        with _make_ws(handler) as c:
            templates = c.templates.list()

        assert captured[0].method == "GET"
        assert captured[0].url.path == "/v1/workspace/templates"
        assert templates
        assert all(isinstance(t, Template) for t in templates)

    def test_non_list_body_defensive(self) -> None:
        with _make_ws(lambda req: httpx.Response(200, json={"templates": []})) as c:
            assert c.templates.list() == []


class TestGet:
    def test_url_and_parse(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("templates_detail"))

        with _make_ws(handler) as c:
            detail = c.templates.get("support-ai-chatbot-agent")

        assert (
            captured[0].url.path == "/v1/workspace/templates/support-ai-chatbot-agent"
        )
        assert isinstance(detail, TemplateDetail)
        assert "instructions" in detail.agent_config

    def test_404_template_not_found(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json=error_envelope("template_not_found"))

        with _make_ws(handler) as c, pytest.raises(TemplateNotFound):
            c.templates.get("nope")
