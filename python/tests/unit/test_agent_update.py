"""Tests for ``UserClient.agents.update()`` and ``list_assignable_models()``.

Each block pins one invariant: which arguments become the request body, which
are refused before any request, how a refused model slot reaches the caller as
``ModelNotAssignable``, and how ``ModelForAgent`` is read. Wire bodies come from
``shared/fixtures/v1/`` so the tests consume the same JSON other language
clients will use. The routes and the generic error and unexpected-success
behaviour are covered in ``test_agent_detail_models.py``.
"""

import json
from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest

from xagent_sdk import (
    AgentDetail,
    AgentNotFound,
    InvalidAPIKey,
    InvalidInput,
    MalformedResponse,
    ModelForAgent,
    ModelInfo,
    ModelNotAssignable,
    UserClient,
)

from ._fixtures import error_envelope, response

_Handler = Callable[[httpx.Request], httpx.Response]


def _make_user(handler: _Handler) -> UserClient:
    return UserClient(
        personal_key="xag_personal_p_s",
        base_url="https://test.example",
        transport=httpx.MockTransport(handler),
    )


def _json_response(body: Any) -> httpx.Response:
    return httpx.Response(
        200,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )


def _for_agent_wire() -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], response("agent_assignable_models"))


def _plain_wire() -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], response("model_list"))


def _capture(captured: list[httpx.Request]) -> _Handler:
    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json=response("agent_update"))

    return handler


def _sent(**kwargs: Any) -> dict[str, Any]:
    """Call ``update(7, **kwargs)`` and return the JSON body that was sent."""
    captured: list[httpx.Request] = []
    with _make_user(_capture(captured)) as c:
        c.agents.update(7, **kwargs)
    assert len(captured) == 1
    return json.loads(captured[0].content)  # type: ignore[no-any-return]


class TestRequestBody:
    @pytest.mark.parametrize(
        ("kwargs", "body"),
        [
            ({}, {}),
            ({"name": "Billing"}, {"name": "Billing"}),
            (
                {"description": "Answers billing questions."},
                {"description": "Answers billing questions."},
            ),
            ({"instructions": "Be brief."}, {"instructions": "Be brief."}),
            ({"models": {"visual": 3}}, {"models": {"visual": 3}}),
            ({"models": {"visual": None}}, {"models": {"visual": None}}),
            ({"models": {}}, {"models": {}}),
            ({"description": ""}, {"description": ""}),
            ({"instructions": ""}, {"instructions": ""}),
            (
                {
                    "name": "A",
                    "description": "B",
                    "instructions": "C",
                    "models": {"general": 1, "compact": None},
                },
                {
                    "name": "A",
                    "description": "B",
                    "instructions": "C",
                    "models": {"general": 1, "compact": None},
                },
            ),
            (
                {"name": "A", "models": {"small_fast": 2}},
                {"name": "A", "models": {"small_fast": 2}},
            ),
        ],
        ids=[
            "nothing",
            "name",
            "description",
            "instructions",
            "one-slot",
            "clear-slot",
            "empty-models",
            "empty-description",
            "empty-instructions",
            "everything",
            "name-and-slot",
        ],
    )
    def test_only_the_arguments_passed_are_sent(
        self, kwargs: dict[str, Any], body: dict[str, Any]
    ) -> None:
        assert _sent(**kwargs) == body

    def test_nothing_passed_sends_an_empty_object_not_an_empty_body(self) -> None:
        captured: list[httpx.Request] = []
        with _make_user(_capture(captured)) as c:
            c.agents.update(7)
        assert captured[0].content == b"{}"
        assert captured[0].headers["content-type"] == "application/json"

    def test_the_models_mapping_is_not_modified(self) -> None:
        slots: dict[str, int | None] = {"general": 3, "compact": None}
        _sent(models=slots)
        assert slots == {"general": 3, "compact": None}

    def test_models_is_copied_into_a_plain_dict_so_any_mapping_type_is_accepted(
        self,
    ) -> None:
        from types import MappingProxyType

        assert _sent(models=MappingProxyType({"general": 3})) == {
            "models": {"general": 3}
        }

    def test_the_agent_id_goes_into_the_path(self) -> None:
        captured: list[httpx.Request] = []
        with _make_user(_capture(captured)) as c:
            c.agents.update(2**31 - 1, name="x")
            c.agents.update(True, name="x")
        assert [r.url.raw_path for r in captured] == [
            b"/v1/agents/2147483647",
            b"/v1/agents/1",
        ]
        assert [r.method for r in captured] == ["PATCH", "PATCH"]


class TestRefusedBeforeAnyRequest:
    @pytest.mark.parametrize(
        "agent_id",
        [".", "..", "5/tools", "1", 1.0, None],
        ids=["dot", "dotdot", "slash", "digit-string", "float", "none"],
    )
    def test_a_non_integer_agent_id_raises_type_error(self, agent_id: Any) -> None:
        captured: list[httpx.Request] = []
        with _make_user(_capture(captured)) as c, pytest.raises(TypeError):
            c.agents.update(agent_id, name="x")
        assert captured == []

    def test_a_positional_field_raises_type_error(self) -> None:
        captured: list[httpx.Request] = []
        with _make_user(_capture(captured)) as c, pytest.raises(TypeError):
            c.agents.update(7, "Billing")  # type: ignore[call-arg]
        assert captured == []

    @pytest.mark.parametrize("field", ["skills", "execution_mode", "visibility"])
    def test_an_unknown_argument_raises_type_error(self, field: str) -> None:
        captured: list[httpx.Request] = []
        with _make_user(_capture(captured)) as c, pytest.raises(TypeError):
            c.agents.update(7, **{field: "x"})  # type: ignore[arg-type]
        assert captured == []

    def test_models_that_is_not_a_mapping_raises_before_any_request(self) -> None:
        captured: list[httpx.Request] = []
        with (
            _make_user(_capture(captured)) as c,
            pytest.raises((TypeError, ValueError)),
        ):
            c.agents.update(7, models=[("general", 1), 5])  # type: ignore[arg-type]
        assert captured == []

    def test_a_models_none_is_not_the_unset_marker(self) -> None:
        # ``None`` for the whole mapping is not allowed; it must not be
        # silently read as "leave unchanged".
        captured: list[httpx.Request] = []
        with _make_user(_capture(captured)) as c, pytest.raises(TypeError):
            c.agents.update(7, models=None)  # type: ignore[arg-type]
        assert captured == []


class TestResult:
    def test_the_result_is_the_agent_as_stored(self) -> None:
        wire = response("agent_update")
        with _make_user(lambda req: httpx.Response(200, json=wire)) as c:
            detail = c.agents.update(1, models={"compact": 1})
        assert isinstance(detail, AgentDetail)
        assert detail.agent_id == wire["id"]
        assert detail.owner_user_id == wire["user_id"]
        assert detail.models == wire["models"]
        assert detail.models is not None
        assert detail.models["compact"] == 1
        assert detail.unresolved_models == wire["unresolved_models"]
        assert detail.updated_at == wire["updated_at"]

    @pytest.mark.parametrize(
        "missing", ["id", "user_id", "name", "models", "unresolved_models"]
    )
    def test_a_body_missing_a_field_is_malformed(self, missing: str) -> None:
        body = {k: v for k, v in response("agent_update").items() if k != missing}
        with (
            _make_user(lambda req: httpx.Response(200, json=body)) as c,
            pytest.raises(MalformedResponse) as exc,
        ):
            c.agents.update(1, name="x")
        assert exc.value.http_status is None

    @pytest.mark.parametrize("body", [[], None, "x", 5, {}])
    def test_a_body_that_is_not_an_agent_is_malformed(self, body: Any) -> None:
        with (
            _make_user(lambda req: _json_response(body)) as c,
            pytest.raises(MalformedResponse),
        ):
            c.agents.update(1, name="x")

    @pytest.mark.parametrize("body", [{}, {"id": "seven"}])
    def test_a_wrong_shaped_200_says_the_server_accepted_the_request(
        self, body: Any
    ) -> None:
        with (
            _make_user(lambda req: _json_response(body)) as c,
            pytest.raises(MalformedResponse) as exc,
        ):
            c.agents.update(1, name="x")
        assert exc.value.http_status is None
        assert "the server accepted the request" in exc.value.message

    def test_get_does_not_claim_the_server_accepted_anything(self) -> None:
        with (
            _make_user(lambda req: _json_response({})) as c,
            pytest.raises(MalformedResponse) as exc,
        ):
            c.agents.get(1)
        assert "accepted" not in exc.value.message

    def test_a_non_json_200_is_malformed(self) -> None:
        with (
            _make_user(
                lambda req: httpx.Response(
                    200, text="<html/>", headers={"content-type": "text/html"}
                )
            ) as c,
            pytest.raises(MalformedResponse) as exc,
        ):
            c.agents.update(1, name="x")
        assert exc.value.http_status is None


class TestErrors:
    def test_401_is_invalid_api_key(self) -> None:
        body = error_envelope("invalid_api_key")
        with (
            _make_user(lambda req: httpx.Response(401, json=body)) as c,
            pytest.raises(InvalidAPIKey) as exc,
        ):
            c.agents.update(1, name="x")
        assert exc.value.http_status == 401

    def test_404_is_agent_not_found(self) -> None:
        body = error_envelope("agent_not_found")
        with (
            _make_user(lambda req: httpx.Response(404, json=body)) as c,
            pytest.raises(AgentNotFound) as exc,
        ):
            c.agents.update(1, name="x")
        assert exc.value.http_status == 404

    def test_422_is_invalid_input(self) -> None:
        body = error_envelope("validation_422")
        with (
            _make_user(lambda req: httpx.Response(422, json=body)) as c,
            pytest.raises(InvalidInput) as exc,
        ):
            c.agents.update(1, name="")
        assert exc.value.http_status == 422
        assert exc.value.details is None

    def test_a_null_field_answer_is_invalid_input_without_details(self) -> None:
        # The type hints exclude ``None`` for these fields, but a caller that
        # passes it anyway gets it sent as ``null``; the answer is mapped.
        body = error_envelope("invalid_input_null_field")
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(400, json=body)

        with _make_user(handler) as c, pytest.raises(InvalidInput) as exc:
            c.agents.update(1, name=None)  # type: ignore[arg-type]
        assert json.loads(captured[0].content) == {"name": None}
        assert exc.value.http_status == 400
        assert "cannot be null" in exc.value.message
        assert exc.value.details is None

    def test_a_duplicate_name_is_invalid_input(self) -> None:
        body = {
            "error": {
                "code": "invalid_input",
                "message": "Agent with this name already exists.",
            }
        }
        with (
            _make_user(lambda req: httpx.Response(400, json=body)) as c,
            pytest.raises(InvalidInput) as exc,
        ):
            c.agents.update(1, name="Taken")
        assert exc.value.http_status == 400
        assert exc.value.details is None


def _refusal(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "error": {
            "code": "model_not_assignable",
            "message": "These model slots cannot use the requested model.",
            "details": {"models": entries},
        }
    }


class TestRejectedModelSlots:
    def _raise(self, body: dict[str, Any]) -> ModelNotAssignable:
        with (
            _make_user(lambda req: httpx.Response(400, json=body)) as c,
            pytest.raises(ModelNotAssignable) as exc,
        ):
            c.agents.update(1, models={"general": 4, "small_fast": 2})
        return exc.value

    def test_the_fixture_has_the_two_reasons(self) -> None:
        wire = error_envelope("model_not_assignable")
        entries = wire["error"]["details"]["models"]
        assert [e["reason"] for e in entries] == ["owner_cannot_use", "not_llm"]
        assert "owner_user_id" in entries[0]
        assert "owner_user_id" not in entries[1]

    def test_details_models_keeps_every_entry_in_order(self) -> None:
        wire = error_envelope("model_not_assignable")
        err = self._raise(wire)
        assert err.http_status == 400
        assert err.code == "model_not_assignable"
        assert err.message == wire["error"]["message"]
        assert err.details == wire["error"]["details"]
        assert err.details is not None
        assert err.details["models"] == [
            {
                "slot": "general",
                "id": 4,
                "reason": "owner_cannot_use",
                "owner_user_id": 2,
            },
            {"slot": "small_fast", "id": 2, "reason": "not_llm"},
        ]

    def test_it_is_not_an_invalid_input(self) -> None:
        # Its own class, as for ``ConnectorNotShared``: ``except InvalidInput``
        # around ``update()`` does not swallow a refused slot.
        err = self._raise(error_envelope("model_not_assignable"))
        assert not isinstance(err, InvalidInput)

    @pytest.mark.parametrize(
        "entry",
        [
            {"slot": "general", "id": 91, "reason": "unavailable"},
            {"slot": "visual", "id": 5, "reason": "not_llm"},
            {
                "slot": "compact",
                "id": 6,
                "reason": "owner_cannot_use",
                "owner_user_id": 19,
            },
            {"slot": "small_fast", "id": 7, "reason": "a_future_reason"},
        ],
        ids=["unavailable", "not_llm", "owner_cannot_use", "unknown-reason"],
    )
    def test_each_reason_reaches_the_caller_unchanged(
        self, entry: dict[str, Any]
    ) -> None:
        err = self._raise(_refusal([entry]))
        assert err.details == {"models": [entry]}

    def test_all_four_slots_can_be_rejected_together(self) -> None:
        entries = [
            {"slot": slot, "id": i, "reason": "unavailable"}
            for i, slot in enumerate(["general", "small_fast", "visual", "compact"])
        ]
        err = self._raise(_refusal(entries))
        assert err.details is not None
        assert [e["slot"] for e in err.details["models"]] == [
            "general",
            "small_fast",
            "visual",
            "compact",
        ]


class TestModelForAgent:
    def test_it_is_a_model_info_with_one_more_field(self) -> None:
        assert issubclass(ModelForAgent, ModelInfo)
        info_fields = set(ModelInfo.__dataclass_fields__)
        assert set(ModelForAgent.__dataclass_fields__) == info_fields | {
            "owner_can_use"
        }

    def test_the_listing_returns_every_field_of_every_model(self) -> None:
        wire = _for_agent_wire()
        assert isinstance(wire, list)
        # Guard: the fixture holds both values of the flag and only llm models.
        assert {m["owner_can_use"] for m in wire} == {True, False}
        assert {m["category"] for m in wire} == {"llm"}
        with _make_user(lambda req: httpx.Response(200, json=wire)) as c:
            models = c.agents.list_assignable_models(1)
        assert [type(m) for m in models] == [ModelForAgent] * len(wire)
        for model, item in zip(models, wire, strict=True):
            for name in [*ModelInfo.__dataclass_fields__, "owner_can_use"]:
                assert getattr(model, name) == item[name], name

    def test_the_server_order_is_kept_and_an_empty_list_is_accepted(self) -> None:
        wire = list(reversed(_for_agent_wire()))
        with _make_user(lambda req: httpx.Response(200, json=wire)) as c:
            assert [m.id for m in c.agents.list_assignable_models(1)] == [
                m["id"] for m in wire
            ]
        with _make_user(lambda req: httpx.Response(200, json=[])) as c:
            assert c.agents.list_assignable_models(1) == []

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, True),
            (False, False),
            (1, True),
            (0, False),
            ("true", True),
            ("false", False),
        ],
        ids=["true", "false", "one", "zero", "string-true", "string-false"],
    )
    def test_owner_can_use_is_read_as_a_bool(self, value: Any, expected: bool) -> None:
        # A value that converts to a bool is read, as in every other parser.
        wire = _for_agent_wire()
        body = [{**wire[0], "owner_can_use": value}]
        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            (model,) = c.agents.list_assignable_models(1)
        assert model.owner_can_use is expected

    def test_a_missing_owner_can_use_is_malformed(self) -> None:
        wire = _for_agent_wire()
        bad = {k: v for k, v in wire[0].items() if k != "owner_can_use"}
        with (
            _make_user(lambda req: httpx.Response(200, json=[bad])) as c,
            pytest.raises(MalformedResponse, match="owner_can_use") as exc,
        ):
            c.agents.list_assignable_models(1)
        assert exc.value.http_status is None

    @pytest.mark.parametrize(
        "value",
        [None, "maybe", 2, [True], {"a": True}],
        ids=["null", "string", "number", "list", "object"],
    )
    def test_an_unreadable_owner_can_use_is_malformed(self, value: Any) -> None:
        wire = _for_agent_wire()
        bad = {**wire[0], "owner_can_use": value}
        with (
            _make_user(lambda req: httpx.Response(200, json=[bad])) as c,
            pytest.raises(MalformedResponse, match="owner_can_use"),
        ):
            c.agents.list_assignable_models(1)

    def test_one_bad_entry_fails_the_whole_call(self) -> None:
        wire = _for_agent_wire()
        bad = {k: v for k, v in wire[0].items() if k != "owner_can_use"}
        body = [wire[1], bad]
        with (
            _make_user(lambda req: httpx.Response(200, json=body)) as c,
            pytest.raises(MalformedResponse),
        ):
            c.agents.list_assignable_models(1)

    @pytest.mark.parametrize(
        "body",
        [{}, None, "x", 5, {"models": []}],
        ids=["object", "null", "string", "number", "wrapped-array"],
    )
    def test_a_body_that_is_not_an_array_is_malformed(self, body: Any) -> None:
        with (
            _make_user(lambda req: _json_response(body)) as c,
            pytest.raises(MalformedResponse, match="expected a JSON array"),
        ):
            c.agents.list_assignable_models(1)

    def test_the_plain_list_does_not_require_owner_can_use(self) -> None:
        # ``GET /v1/models`` sends no such field.
        with _make_user(lambda req: httpx.Response(200, json=_plain_wire())) as c:
            models = c.models.list()
        assert [type(m) for m in models] == [ModelInfo] * len(models)
        assert not any(hasattr(m, "owner_can_use") for m in models)

    def test_the_results_are_frozen(self) -> None:
        with _make_user(lambda req: httpx.Response(200, json=_for_agent_wire())) as c:
            model = c.agents.list_assignable_models(1)[0]
        with pytest.raises(AttributeError):
            model.owner_can_use = True  # type: ignore[misc]


class TestAssignableModelsRoute:
    @pytest.mark.parametrize(
        ("agent_id", "path"),
        [
            (1, b"/v1/agents/1/assignable-models"),
            (2**31 - 1, b"/v1/agents/2147483647/assignable-models"),
            (True, b"/v1/agents/1/assignable-models"),
        ],
        ids=["one", "max", "bool"],
    )
    def test_the_agent_id_goes_into_the_path_and_nothing_else_is_sent(
        self, agent_id: int, path: bytes
    ) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=[])

        with _make_user(handler) as c:
            c.agents.list_assignable_models(agent_id)
        assert captured[0].method == "GET"
        assert captured[0].url.raw_path == path
        assert captured[0].url.query == b""
        assert captured[0].content == b""

    def test_the_plain_list_has_no_query(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=[])

        with _make_user(handler) as c:
            c.models.list()
        assert captured[0].url.raw_path == b"/v1/models"

    @pytest.mark.parametrize(
        "agent_id",
        [".", "..", "7/../me", "7", "7?x=1", 1.5, None],
        ids=["dot", "dotdot", "path", "digit-string", "query", "float", "none"],
    )
    def test_a_non_integer_agent_id_raises_before_any_request(
        self, agent_id: Any
    ) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=[])

        with _make_user(handler) as c, pytest.raises(TypeError):
            c.agents.list_assignable_models(agent_id)
        assert captured == []

    def test_a_keyword_agent_id_is_accepted(self) -> None:
        with _make_user(lambda req: httpx.Response(200, json=[])) as c:
            assert c.agents.list_assignable_models(agent_id=1) == []

    def test_404_is_agent_not_found(self) -> None:
        body = error_envelope("agent_not_found")
        with (
            _make_user(lambda req: httpx.Response(404, json=body)) as c,
            pytest.raises(AgentNotFound) as exc,
        ):
            c.agents.list_assignable_models(1)
        assert exc.value.http_status == 404

    def test_422_is_invalid_input(self) -> None:
        body = error_envelope("validation_422")
        with (
            _make_user(lambda req: httpx.Response(422, json=body)) as c,
            pytest.raises(InvalidInput) as exc,
        ):
            c.agents.list_assignable_models(0)
        assert exc.value.http_status == 422

    def test_401_is_invalid_api_key(self) -> None:
        body = error_envelope("invalid_api_key")
        with (
            _make_user(lambda req: httpx.Response(401, json=body)) as c,
            pytest.raises(InvalidAPIKey),
        ):
            c.agents.list_assignable_models(1)


class TestDocumentation:
    def test_update_docstring_lists_every_reason_and_the_fallback(self) -> None:
        from xagent_sdk._agents import AgentsAPI

        doc = AgentsAPI.update.__doc__ or ""
        flat = " ".join(doc.split())
        for reason in ("unavailable", "not_llm", "owner_cannot_use", "owner_user_id"):
            assert reason in doc
        assert "Treat any other string" in doc
        assert "already running can keep using the previous values" in flat
        assert "workforce_config_changed" in doc
        assert 'details["models"]' in doc
        assert "ModelNotAssignable" in doc
        assert '{"slot": str, "id": int, "reason": str}' in flat
        assert "list_assignable_models()" in doc
        assert "falls back to a default model for that slot without an error" in flat
        assert "such a ``general`` model fails the call" in flat

    def test_readme_lists_the_new_methods_and_reasons(self) -> None:
        from pathlib import Path

        text = (Path(__file__).resolve().parents[2] / "README.md").read_text()
        flat = " ".join(text.split())
        assert "| `user.agents.update(agent_id," in text
        assert "| `user.agents.list_assignable_models(agent_id)` |" in text
        assert "ModelForAgent" in text
        assert "| `ModelNotAssignable` | 400 | `model_not_assignable`" in text
        assert "already running can keep using the previous values" in flat
        assert "falls back to a default model for that slot without an error" in flat
        for reason in ("unavailable", "not_llm", "owner_cannot_use"):
            assert f"`{reason}`" in text
