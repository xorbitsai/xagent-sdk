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
        assert len(templates) == 4
        assert all(isinstance(t, Template) for t in templates)
        # Spot-check a couple
        ids = [t.template_id for t in templates]
        assert "content_generator" in ids
        assert "q_and_a" in ids

    def test_empty_list(self) -> None:
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"templates": []})

        with _make_user(h) as c:
            assert c.templates.list() == []

    def test_non_dict_body_defensive(self) -> None:
        # Mirrors _parse_steps: malformed body returns [] rather than
        # raising AttributeError.
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        with _make_user(h) as c:
            assert c.templates.list() == []


class TestGet:
    def test_url_and_parse(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("templates_detail"))

        with _make_user(handler) as c:
            detail = c.templates.get("q_and_a")

        assert captured[0].method == "GET"
        assert captured[0].url.path == "/v1/templates/q_and_a"
        assert isinstance(detail, TemplateDetail)
        assert detail.template_id == "q_and_a"
        assert detail.name == "Q&A"
        # agent_config is the merge target for create_from_template overrides
        assert "instructions" in detail.agent_config
        assert detail.agent_config["mode"] == "balanced"

    def test_404_template_not_found(self) -> None:
        with _make_user(_404_handler) as c, pytest.raises(TemplateNotFound):
            c.templates.get("not_a_real_template")
