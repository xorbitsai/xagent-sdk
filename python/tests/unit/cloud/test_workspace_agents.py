"""Tests for WorkspaceClient.agents (WorkspaceAgentsAPI)."""

import inspect
import json

import httpx
import pytest

from xagent_sdk import (
    AgentCreateResult,
    AgentNotFound,
    AgentSummary,
    MalformedResponse,
    TemplateNotFound,
)
from xagent_sdk.cloud import WorkspaceClient
from xagent_sdk.cloud._agents import WorkspaceAgentsAPI

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
            return httpx.Response(200, json=response("agents_list"))

        with _make_ws(handler) as c:
            agents = c.agents.list()

        assert captured[0].method == "GET"
        assert captured[0].url.path == "/v1/workspace/agents"
        assert agents
        assert all(isinstance(a, AgentSummary) for a in agents)

    def test_empty_list(self) -> None:
        with _make_ws(lambda req: httpx.Response(200, json=[])) as c:
            assert c.agents.list() == []

    def test_non_list_body_defensive(self) -> None:
        with _make_ws(lambda req: httpx.Response(200, json={"agents": []})) as c:
            assert c.agents.list() == []


class TestCreate:
    def test_body_default(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(201, json=response("agents_create"))

        with _make_ws(handler) as c:
            result = c.agents.create(name="HR Bot", instructions="help")

        assert captured[0].method == "POST"
        assert captured[0].url.path == "/v1/workspace/agents"
        body = json.loads(captured[0].content)
        assert body == {
            "name": "HR Bot",
            "instructions": "help",
            "generate_runtime_key": True,
        }
        assert isinstance(result, AgentCreateResult)
        assert result.runtime_full_key is not None

    def test_optional_fields_spread_no_none(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(201, json=response("agents_create"))

        with _make_ws(handler) as c:
            c.agents.create(
                name="X",
                instructions="i",
                description="d",
                execution_mode="flash",
                tool_categories=["basic"],
                models=None,  # stays out of the body
            )

        body = json.loads(captured[0].content)
        assert body["description"] == "d"
        assert body["execution_mode"] == "flash"
        assert body["tool_categories"] == ["basic"]
        assert "models" not in body  # None dropped
        assert "metadata" not in body

    def test_create_has_no_metadata_param(self) -> None:
        params = inspect.signature(WorkspaceAgentsAPI.create).parameters
        assert "metadata" not in params

    def test_list_fields_are_lists_not_sequences(self) -> None:
        # A bare str satisfies Sequence[str] and would reach the wire as a
        # scalar; the list-valued params must be typed as concrete lists so
        # type checking rejects a stray string. (Annotations are strings
        # here because the module uses ``from __future__ import annotations``.)
        params = inspect.signature(WorkspaceAgentsAPI.create).parameters
        for field in (
            "knowledge_bases",
            "skills",
            "tool_categories",
            "suggested_prompts",
        ):
            ann = params[field].annotation
            assert "Sequence" not in ann, f"{field} must not admit a bare str: {ann}"

    def test_fails_closed_without_key(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"agent": {"id": 1, "name": "X"}})

        with _make_ws(handler) as c, pytest.raises(MalformedResponse) as exc:
            c.agents.create(name="X", instructions="i")
        assert exc.value.code == "malformed_response"


class TestCreateFromTemplate:
    def test_flat_body_no_overrides_wrapper(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(201, json=response("agents_create"))

        with _make_ws(handler) as c:
            c.agents.create_from_template("support-ai-chatbot-agent", name="HR Bot")

        assert captured[0].url.path == "/v1/workspace/agents/from-template"
        body = json.loads(captured[0].content)
        assert body == {
            "template_id": "support-ai-chatbot-agent",
            "generate_runtime_key": True,
            "name": "HR Bot",
        }
        assert "overrides" not in body

    def test_template_not_found(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json=error_envelope("template_not_found"))

        with _make_ws(handler) as c, pytest.raises(TemplateNotFound):
            c.agents.create_from_template("nope")

    def test_fails_closed_without_key(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"agent": {"id": 1, "name": "X"}})

        with _make_ws(handler) as c, pytest.raises(MalformedResponse):
            c.agents.create_from_template("t")


class TestRotateKey:
    def test_url_and_parse(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("rotate_key"))

        with _make_ws(handler) as c:
            result = c.agents.rotate_key(42)

        assert captured[0].method == "POST"
        assert captured[0].url.path == "/v1/workspace/agents/42/api-key"
        assert result.full_key.startswith("xag_")

    def test_agent_not_found(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json=error_envelope("agent_not_found"))

        with _make_ws(handler) as c, pytest.raises(AgentNotFound):
            c.agents.rotate_key(99999999)
