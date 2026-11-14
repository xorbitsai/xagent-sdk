"""Tests for WorkspaceClient.agents (WorkspaceAgentsAPI)."""

import inspect
import json
from typing import Any

import httpx
import pytest

from xagent_sdk import (
    AgentCreateResult,
    AgentNotFound,
    AgentSummary,
    APIKeyNotFound,
    MalformedResponse,
    RevokeKeyResult,
    RuntimeKeyInfo,
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
            return httpx.Response(200, json=response("agents_create"))

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
            return httpx.Response(200, json=response("agents_create"))

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
            return httpx.Response(200, json={"agent": {"id": 1, "name": "X"}})

        with _make_ws(handler) as c, pytest.raises(MalformedResponse) as exc:
            c.agents.create(name="X", instructions="i")
        assert exc.value.code == "malformed_response"
        assert "carried no runtime key" in exc.value.message
        assert exc.value.http_status is None


class TestCreateFromTemplate:
    def test_flat_body_no_overrides_wrapper(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("agents_create"))

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
            return httpx.Response(200, json={"agent": {"id": 1, "name": "X"}})

        with _make_ws(handler) as c, pytest.raises(MalformedResponse) as exc:
            c.agents.create_from_template("t")
        assert exc.value.code == "malformed_response"
        assert "carried no runtime key" in exc.value.message
        assert exc.value.http_status is None


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


class TestRuntimeKeys:
    @pytest.mark.parametrize(
        ("include_revoked", "query"),
        [(False, b""), (True, b"include_revoked=true")],
    )
    def test_list_keys_url_and_parse(self, include_revoked: bool, query: bytes) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("runtime_keys_list"))

        with _make_ws(handler) as c:
            keys = c.agents.list_keys(42, include_revoked=include_revoked)

        assert captured[0].method == "GET"
        assert captured[0].url.path == "/v1/workspace/agents/42/api-keys"
        assert captured[0].url.query == query
        assert [type(key) for key in keys] == [RuntimeKeyInfo, RuntimeKeyInfo]
        assert [(k.key_id, k.status, k.label) for k in keys] == [
            (12, "active", None),
            (7, "paused", "ci"),
        ]
        assert keys[0].key_prefix == "newabc"
        assert keys[0].masked_key.startswith("xag_newabc_")
        assert keys[0].created_at.year == 2026
        assert keys[0].last_used_at is not None
        assert keys[1].last_used_at is None
        assert keys[1].revoked_at is None

    def test_list_keys_empty(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        with _make_ws(handler) as c:
            assert c.agents.list_keys(42) == []

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({}, id="object"),
            pytest.param({"keys": []}, id="wrapped"),
            pytest.param(None, id="null"),
            pytest.param(["xag_newabc"], id="item-not-object"),
            pytest.param([{"key_prefix": "newabc"}], id="item-missing-fields"),
        ],
    )
    def test_list_keys_never_reads_a_wrong_body_as_empty(self, body: Any) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        with _make_ws(handler) as c, pytest.raises(MalformedResponse):
            c.agents.list_keys(42)

    def test_revoke_key_url_and_parse(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("runtime_key_revoke"))

        with _make_ws(handler) as c:
            result = c.agents.revoke_key(42, 12)

        assert captured[0].method == "DELETE"
        assert captured[0].url.path == "/v1/workspace/agents/42/api-keys/12"
        assert captured[0].content == b""
        assert isinstance(result, RevokeKeyResult)
        assert (result.key_id, result.key_prefix, result.revoked) == (
            12,
            "newabc",
            True,
        )
        assert result.revoked_at.year == 2026

    def test_revoke_key_reports_an_already_revoked_key(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={**response("runtime_key_revoke"), "revoked": False}
            )

        with _make_ws(handler) as c:
            assert c.agents.revoke_key(42, 12).revoked is False

    def test_revoke_key_wrong_body_is_malformed(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"deleted": True})

        with _make_ws(handler) as c, pytest.raises(MalformedResponse):
            c.agents.revoke_key(42, 12)

    @pytest.mark.parametrize(
        ("fixture", "expected"),
        [("agent_not_found", AgentNotFound), ("api_key_not_found", APIKeyNotFound)],
    )
    def test_not_found_codes_map_to_distinct_errors(
        self, fixture: str, expected: type[Exception]
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json=error_envelope(fixture))

        with _make_ws(handler) as c:
            with pytest.raises(expected):
                c.agents.revoke_key(42, 12)
            if expected is AgentNotFound:
                with pytest.raises(AgentNotFound):
                    c.agents.list_keys(42)
