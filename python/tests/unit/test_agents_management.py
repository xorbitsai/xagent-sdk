"""Tests for UserClient.agents (AgentsAPI)."""

import json
from typing import Any

import httpx
import pytest

from xagent_sdk import (
    AgentCreateResult,
    AgentNotFound,
    AgentSkillsUpdate,
    AgentSummary,
    APIKeyNotFound,
    InvalidInput,
    MalformedResponse,
    RevokeKeyResult,
    RotateKeyResult,
    RuntimeKeyInfo,
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
            return httpx.Response(200, json=response("agents_create"))

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
                200,
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
                200, json={"agent": {"id": 42, "name": "HR Leave Assistant"}}
            )

        with _make_user(handler) as c, pytest.raises(MalformedResponse) as excinfo:
            c.agents.create(name="HR Leave Assistant", instructions="...")
        assert excinfo.value.code == "malformed_response"
        assert "carried no runtime key" in excinfo.value.message
        assert excinfo.value.http_status is None

    def test_generate_runtime_key_true_but_empty_key_fails_closed(self) -> None:
        # Empty string is also not a usable runtime credential; create()
        # should surface the malformed backend response immediately.
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "agent": {"id": 42, "name": "HR Leave Assistant"},
                    "api_key": {"full_key": "", "key_prefix": "abc123"},
                },
            )

        with _make_user(handler) as c, pytest.raises(MalformedResponse) as excinfo:
            c.agents.create(name="HR Leave Assistant", instructions="...")
        assert excinfo.value.code == "malformed_response"
        assert "carried no runtime key" in excinfo.value.message
        assert excinfo.value.http_status is None

    def test_metadata_included_when_given(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("agents_create"))

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
            return httpx.Response(200, json=response("agents_create"))

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
            return httpx.Response(200, json=response("agents_create"))

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
            return httpx.Response(200, json=response("agents_create"))

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
            return httpx.Response(200, json=response("agents_create"))

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
            return httpx.Response(200, json={"agent": {"id": 42, "name": "X"}})

        with _make_user(h) as c, pytest.raises(MalformedResponse) as excinfo:
            c.agents.create_from_template("q_and_a")
        assert excinfo.value.code == "malformed_response"
        assert "carried no runtime key" in excinfo.value.message
        assert excinfo.value.http_status is None

    def test_generate_runtime_key_true_but_empty_key_fails_closed(self) -> None:
        # Same fail-closed contract as create(): empty full_key is not a
        # usable one-time runtime key.
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "agent": {"id": 42, "name": "X"},
                    "api_key": {"full_key": "", "key_prefix": "abc123"},
                },
            )

        with _make_user(h) as c, pytest.raises(MalformedResponse) as excinfo:
            c.agents.create_from_template("q_and_a")
        assert excinfo.value.code == "malformed_response"
        assert "carried no runtime key" in excinfo.value.message
        assert excinfo.value.http_status is None


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

        with _make_user(handler) as c:
            keys = c.agents.list_keys(42, include_revoked=include_revoked)

        assert captured[0].method == "GET"
        assert captured[0].url.path == "/v1/agents/42/api-keys"
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

        with _make_user(handler) as c:
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

        with _make_user(handler) as c, pytest.raises(MalformedResponse):
            c.agents.list_keys(42)

    def test_revoke_key_url_and_parse(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("runtime_key_revoke"))

        with _make_user(handler) as c:
            result = c.agents.revoke_key(42, 12)

        assert captured[0].method == "DELETE"
        assert captured[0].url.path == "/v1/agents/42/api-keys/12"
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

        with _make_user(handler) as c:
            assert c.agents.revoke_key(42, 12).revoked is False

    def test_revoke_key_wrong_body_is_malformed(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"deleted": True})

        with _make_user(handler) as c, pytest.raises(MalformedResponse):
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

        with _make_user(handler) as c:
            with pytest.raises(expected):
                c.agents.revoke_key(42, 12)
            if expected is AgentNotFound:
                with pytest.raises(AgentNotFound):
                    c.agents.list_keys(42)


class TestAgentSkills:
    @pytest.mark.parametrize(
        ("skills", "expected_body"),
        [
            pytest.param(["b", "a", "b"], {"skills": ["b", "a", "b"]}, id="duplicates"),
            pytest.param([], {"skills": []}, id="empty"),
            pytest.param("abc", None, id="string"),
        ],
    )
    def test_replace_skills_input_shapes(
        self, skills: Any, expected_body: dict[str, Any] | None
    ) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("agent_skills_update"))

        with _make_user(handler) as c:
            if expected_body is None:
                with pytest.raises(TypeError, match="not a single string"):
                    c.agents.replace_skills(7, skills)
                assert captured == []
            else:
                c.agents.replace_skills(7, skills)
                assert len(captured) == 1
                assert json.loads(captured[0].content) == expected_body

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            pytest.param({}, None, id="empty-object"),
            pytest.param({"agent_id": 7}, None, id="skills-missing"),
            pytest.param({"agent_id": 7, "skills": None}, None, id="skills-null"),
            pytest.param({"agent_id": 7, "skills": "a"}, None, id="skills-string"),
            pytest.param({"agent_id": 7, "skills": [1]}, None, id="skills-not-strings"),
            pytest.param({"skills": []}, None, id="agent-id-missing"),
            pytest.param([], None, id="array"),
            pytest.param({"agent_id": 7, "skills": []}, [], id="real-empty"),
        ],
    )
    def test_get_skills_malformed_bodies(self, body: Any, expected: Any) -> None:
        # A body of the wrong shape must never read as "the agent has no
        # skills": the list is usually edited and written straight back.
        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            if expected is None:
                with pytest.raises(MalformedResponse) as exc:
                    c.agents.get_skills(7)
                assert exc.value.http_status is None
            else:
                assert c.agents.get_skills(7) == expected

    def test_get_skills_returns_names_in_server_order(self) -> None:
        fixture = response("agent_skills")
        with _make_user(lambda req: httpx.Response(200, json=fixture)) as c:
            assert c.agents.get_skills(7) == fixture["skills"]

    @pytest.mark.parametrize("method", ["get_skills", "replace_skills"])
    def test_agent_id_is_encoded_into_the_path(self, method: str) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            if method == "get_skills":
                return httpx.Response(200, json=response("agent_skills"))
            return httpx.Response(200, json=response("agent_skills_update"))

        with _make_user(handler) as c:
            if method == "get_skills":
                c.agents.get_skills("7/../8")  # type: ignore[arg-type]
            else:
                c.agents.replace_skills("7/../8", [])  # type: ignore[arg-type]

        assert len(captured) == 1
        assert captured[0].url.raw_path == b"/v1/agents/7%2F..%2F8/skills"

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(response("agent_skills_update"), id="fixture"),
            pytest.param({"agent_id": 7, "skills": []}, id="missing-unresolved"),
            pytest.param(
                {"agent_id": 7, "unresolved_for_caller": []}, id="missing-skills"
            ),
        ],
    )
    def test_replace_skills_parses_update(self, body: dict[str, Any]) -> None:
        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            if "unresolved_for_caller" not in body or "skills" not in body:
                with pytest.raises(MalformedResponse):
                    c.agents.replace_skills(7, [])
                return
            result = c.agents.replace_skills(7, ["weekly-report"])
        fixture = response("agent_skills_update")
        assert result == AgentSkillsUpdate(**fixture)
        assert result.agent_id == fixture["agent_id"]
        assert result.skills == fixture["skills"]
        assert result.unresolved_for_caller == fixture["unresolved_for_caller"]
        assert result.unresolved_for_caller
