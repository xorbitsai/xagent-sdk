"""Tests for UserClient.agents (AgentsAPI)."""

import json

import httpx
import pytest

from xagent_sdk import (
    AgentCreateResult,
    AgentNotFound,
    AgentSummary,
    InvalidInput,
    RotateKeyResult,
    TemplateNotFound,
    UserClient,
)

from ._fixtures import error_envelope, response


def _make_user(handler: object) -> UserClient:
    return UserClient(
        personal_key="xag_personal_p_s",
        base_url="https://test.example",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


class TestList:
    def test_url_and_parse(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("agents_list"))

        with _make_user(handler) as c:
            agents = c.agents.list()

        assert captured[0].method == "GET"
        assert captured[0].url.path == "/v1/agents"
        assert len(agents) == 3
        assert all(isinstance(a, AgentSummary) for a in agents)
        # Covers the status values backend exposes.
        statuses = {a.status for a in agents}
        assert {"active", "draft", "paused"} <= statuses

    def test_empty_list(self) -> None:
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"agents": []})

        with _make_user(h) as c:
            assert c.agents.list() == []


class TestCreate:
    def test_body_shape_default_generate_key(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(201, json=response("agents_create"))

        with _make_user(handler) as c:
            result = c.agents.create(
                name="HR Leave Assistant",
                instructions="You are an HR assistant. ...",
            )

        assert captured[0].method == "POST"
        assert captured[0].url.path == "/v1/agents"
        body = json.loads(captured[0].content)
        # generate_runtime_key default is True; must appear in body.
        assert body == {
            "name": "HR Leave Assistant",
            "instructions": "You are an HR assistant. ...",
            "generate_runtime_key": True,
        }
        assert isinstance(result, AgentCreateResult)
        assert result.agent_id == 42
        # one-time runtime key carried back
        assert result.runtime_full_key is not None
        assert result.runtime_full_key.startswith("xag_")
        assert result.runtime_key_prefix == "abc123"

    def test_generate_runtime_key_false_passes_through(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(
                201,
                json={
                    "agent_id": 42,
                    "name": "HR Leave Assistant",
                    "runtime_full_key": None,
                    "runtime_key_prefix": None,
                },
            )

        with _make_user(handler) as c:
            result = c.agents.create(
                name="HR Leave Assistant",
                instructions="...",
                generate_runtime_key=False,
            )

        body = json.loads(captured[0].content)
        assert body["generate_runtime_key"] is False
        assert result.runtime_full_key is None
        assert result.runtime_key_prefix is None

    def test_metadata_included_when_given(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(201, json=response("agents_create"))

        with _make_user(handler) as c:
            c.agents.create(
                name="X",
                instructions="...",
                metadata={"trace_id": "abc"},
            )

        body = json.loads(captured[0].content)
        assert body["metadata"] == {"trace_id": "abc"}

    def test_no_metadata_field_when_none(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(201, json=response("agents_create"))

        with _make_user(handler) as c:
            c.agents.create(name="X", instructions="...")

        body = json.loads(captured[0].content)
        assert "metadata" not in body

    def test_422_invalid_input(self) -> None:
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json=error_envelope("validation_422"))

        with _make_user(h) as c, pytest.raises(InvalidInput):
            c.agents.create(name="", instructions="")


class TestCreateFromTemplate:
    def test_body_shape(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(201, json=response("agents_create"))

        with _make_user(handler) as c:
            c.agents.create_from_template(
                "q_and_a",
                overrides={"name": "HR Bot"},
            )

        assert captured[0].method == "POST"
        assert captured[0].url.path == "/v1/agents/from-template"
        body = json.loads(captured[0].content)
        assert body == {
            "template_id": "q_and_a",
            "generate_runtime_key": True,
            "overrides": {"name": "HR Bot"},
        }

    def test_no_overrides_field_when_none(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(201, json=response("agents_create"))

        with _make_user(handler) as c:
            c.agents.create_from_template("q_and_a")

        body = json.loads(captured[0].content)
        assert "overrides" not in body

    def test_template_not_found(self) -> None:
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json=error_envelope("template_not_found"))

        with _make_user(h) as c, pytest.raises(TemplateNotFound):
            c.agents.create_from_template("nope")


class TestRotateKey:
    def test_url_and_parse(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("rotate_key"))

        with _make_user(handler) as c:
            result = c.agents.rotate_key(42)

        assert captured[0].method == "POST"
        assert captured[0].url.path == "/v1/agents/42/api-key"
        assert isinstance(result, RotateKeyResult)
        assert result.full_key.startswith("xag_")
        assert result.key_prefix == "newabc"
        assert result.created_at.year == 2026

    def test_404_agent_not_found(self) -> None:
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json=error_envelope("agent_not_found"))

        with _make_user(h) as c, pytest.raises(AgentNotFound):
            c.agents.rotate_key(99999)
