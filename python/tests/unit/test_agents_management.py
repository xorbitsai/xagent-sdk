"""Tests for UserClient.agents (AgentsAPI)."""

import json

import httpx
import pytest

from xagent_sdk import (
    AgentCreateResult,
    AgentNotFound,
    AgentSummary,
    InvalidInput,
    MalformedResponse,
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
        # Canonical empty: backend returns a bare empty array.
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        with _make_user(h) as c:
            assert c.agents.list() == []

    def test_non_list_body_defensive(self) -> None:
        # Malformed body (dict instead of list) returns [] rather than
        # raising. Mirrors _parse_steps' defense-in-depth pattern.
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
            # When generate_runtime_key=False the backend omits the
            # api_key block; only the agent block is present.
            return httpx.Response(
                201,
                json={
                    "agent": {
                        "id": 42,
                        "name": "HR Leave Assistant",
                    },
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

    def test_generate_runtime_key_true_but_no_key_fails_closed(self) -> None:
        # Backend violated the contract: generate_runtime_key=True was
        # requested but the response carried no api_key block. The SDK
        # must refuse rather than return runtime_full_key=None, which
        # would let AgentClient(api_key=None) silently fall back to
        # XAGENT_API_KEY.
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                201, json={"agent": {"id": 42, "name": "HR Leave Assistant"}}
            )

        with _make_user(handler) as c, pytest.raises(MalformedResponse) as excinfo:
            c.agents.create(name="HR Leave Assistant", instructions="...")
        assert excinfo.value.code == "malformed_response"

    def test_generate_runtime_key_true_but_empty_key_fails_closed(self) -> None:
        # Empty string is also not a usable runtime credential; create()
        # should surface the malformed backend response immediately.
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                201,
                json={
                    "agent": {"id": 42, "name": "HR Leave Assistant"},
                    "api_key": {"full_key": "", "key_prefix": "abc123"},
                },
            )

        with _make_user(handler) as c, pytest.raises(MalformedResponse) as excinfo:
            c.agents.create(name="HR Leave Assistant", instructions="...")
        assert excinfo.value.code == "malformed_response"

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
        # Overrides spread flat alongside template_id (matches backend
        # V1AgentTemplateCreateRequest schema).
        assert body == {
            "template_id": "q_and_a",
            "generate_runtime_key": True,
            "name": "HR Bot",
        }

    def test_no_override_fields_when_none(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(201, json=response("agents_create"))

        with _make_user(handler) as c:
            c.agents.create_from_template("q_and_a")

        body = json.loads(captured[0].content)
        assert body == {
            "template_id": "q_and_a",
            "generate_runtime_key": True,
        }

    def test_template_id_wins_over_collision_in_overrides(self) -> None:
        # Defensive: a caller who passes template_id inside overrides
        # should not be able to override the explicit positional arg.
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(201, json=response("agents_create"))

        with _make_user(handler) as c:
            c.agents.create_from_template(
                "real_template",
                overrides={"template_id": "hijacked", "name": "X"},
            )

        body = json.loads(captured[0].content)
        assert body["template_id"] == "real_template"
        assert body["name"] == "X"

    def test_template_not_found(self) -> None:
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json=error_envelope("template_not_found"))

        with _make_user(h) as c, pytest.raises(TemplateNotFound):
            c.agents.create_from_template("nope")

    def test_generate_runtime_key_true_but_no_key_fails_closed(self) -> None:
        # Same fail-closed contract as create(): default
        # generate_runtime_key=True with a keyless response must raise.
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"agent": {"id": 42, "name": "X"}})

        with _make_user(h) as c, pytest.raises(MalformedResponse) as excinfo:
            c.agents.create_from_template("q_and_a")
        assert excinfo.value.code == "malformed_response"

    def test_generate_runtime_key_true_but_empty_key_fails_closed(self) -> None:
        # Same fail-closed contract as create(): empty full_key is not a
        # usable one-time runtime key.
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                201,
                json={
                    "agent": {"id": 42, "name": "X"},
                    "api_key": {"full_key": "", "key_prefix": "abc123"},
                },
            )

        with _make_user(h) as c, pytest.raises(MalformedResponse) as excinfo:
            c.agents.create_from_template("q_and_a")
        assert excinfo.value.code == "malformed_response"


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
