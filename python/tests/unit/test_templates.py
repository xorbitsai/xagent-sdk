"""Tests for UserClient.templates (TemplatesAPI)."""

import httpx
import pytest

from xagent_sdk import Template, TemplateDetail, TemplateNotFound, UserClient

from ._fixtures import error_envelope, response


def _list_handler(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=response("templates_list"))


def _detail_handler(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=response("templates_detail"))


def _404_handler(req: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json=error_envelope("template_not_found"))


def _make_user(handler: object) -> UserClient:
    return UserClient(
        personal_key="xag_personal_p_s",
        base_url="https://test.example",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


class TestList:
    def test_url(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("templates_list"))

        with _make_user(handler) as c:
            templates = c.templates.list()

        assert captured[0].method == "GET"
        assert captured[0].url.path == "/v1/templates"
        assert len(templates) == 3
        assert all(isinstance(t, Template) for t in templates)
        ids = [t.template_id for t in templates]
        assert "support-email-agent" in ids
        assert "support-ai-chatbot-agent" in ids

    def test_empty_list(self) -> None:
        # Canonical empty: backend returns a bare empty array.
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        with _make_user(h) as c:
            assert c.templates.list() == []

    def test_non_list_body_defensive(self) -> None:
        # Malformed body (dict instead of list) returns [] rather than
        # raising. Mirrors _parse_steps' defense-in-depth pattern.
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"templates": []})

        with _make_user(h) as c:
            assert c.templates.list() == []


class TestGet:
    def test_url_and_parse(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("templates_detail"))

        with _make_user(handler) as c:
            detail = c.templates.get("support-ai-chatbot-agent")

        assert captured[0].method == "GET"
        assert captured[0].url.path == "/v1/templates/support-ai-chatbot-agent"
        assert isinstance(detail, TemplateDetail)
        assert detail.template_id == "support-ai-chatbot-agent"
        assert detail.name == "AI Chatbot Agent"
        # agent_config is the merge target for create_from_template overrides
        assert "instructions" in detail.agent_config
        assert detail.agent_config["execution_mode"] == "flash"

    def test_404_template_not_found(self) -> None:
        with _make_user(_404_handler) as c, pytest.raises(TemplateNotFound):
            c.templates.get("not_a_real_template")

    def test_template_id_is_path_encoded(self) -> None:
        # A slash / query char in the id must stay inside one path segment,
        # not split the route or leak into the query string.
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("templates_detail"))

        with _make_user(handler) as c:
            c.templates.get("a/b?x=1")

        url = captured[0].url
        assert url.query == b""
        assert str(url).endswith("/v1/templates/a%2Fb%3Fx%3D1")
