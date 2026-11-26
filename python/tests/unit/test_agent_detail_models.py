"""Tests for ``agents.get()``, ``models.list()`` and the shared route checks.

``agents.update()`` and ``agents.list_assignable_models()`` are registered here
for the route and error-mapping checks and tested in ``test_agent_update.py``.

Each block pins one invariant: how ``agent_id`` becomes a path, which
arguments are refused before any request, how the wire fields map onto
``AgentDetail`` and ``ModelInfo``, how a body of the wrong shape is rejected,
and which error class each failure maps to. Wire bodies come from
``shared/fixtures/v1/`` so the tests consume the same JSON other language
clients will use.
"""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from xagent_sdk import (
    AgentDetail,
    AgentNotFound,
    InternalError,
    InvalidAPIKey,
    InvalidInput,
    MalformedResponse,
    ModelInfo,
    UserClient,
    XAgentError,
)
from xagent_sdk._models import ModelsAPI

from ._fixtures import error_envelope, response

_Handler = Callable[[httpx.Request], httpx.Response]
_Call = Callable[[UserClient], object]


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


_CALLS: dict[tuple[str, str], _Call] = {
    ("agents", "get"): lambda u: u.agents.get(7),
    ("agents", "update"): lambda u: u.agents.update(7, name="Renamed"),
    ("models", "list"): lambda u: u.models.list(),
    ("agents", "list_assignable_models"): lambda u: u.agents.list_assignable_models(7),
}
_FIXTURES = {
    ("agents", "get"): "agent_detail",
    ("agents", "update"): "agent_update",
    ("models", "list"): "model_list",
    ("agents", "list_assignable_models"): "agent_assignable_models",
}
# HTTP method and raw request path of each canonical call.
_ROUTES: dict[tuple[str, str], tuple[str, bytes]] = {
    ("agents", "get"): ("GET", b"/v1/agents/7"),
    ("agents", "update"): ("PATCH", b"/v1/agents/7"),
    ("models", "list"): ("GET", b"/v1/models"),
    ("agents", "list_assignable_models"): (
        "GET",
        b"/v1/agents/7/assignable-models",
    ),
}
# Request body of each canonical call; a call that sends none is not listed.
_BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("agents", "update"): {"name": "Renamed"},
}
_KEYS = list(_CALLS)
_IDS = [f"{ns}.{m}" for ns, m in _KEYS]

# Wire field -> SDK attribute, for every field of the agent body.
_AGENT_WIRE_FIELDS = [
    "id",
    "user_id",
    "team_id",
    "visibility",
    "name",
    "description",
    "instructions",
    "execution_mode",
    "models",
    "unresolved_models",
    "knowledge_bases",
    "skills",
    "tool_categories",
    "suggested_prompts",
    "prompt_context_fields",
    "logo_url",
    "status",
    "published_at",
    "created_at",
    "updated_at",
    "widget_enabled",
    "allowed_domains",
    "share_enabled",
    "share_updated_at",
]
_MODEL_FIELDS = [
    "id",
    "model_id",
    "model_name",
    "model_provider",
    "category",
    "abilities",
    "is_platform",
    "owned_by_you",
]


class TestRoutes:
    @pytest.mark.parametrize("key", _KEYS, ids=_IDS)
    def test_methods_send_expected_method_path_and_body(
        self, key: tuple[str, str]
    ) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response(_FIXTURES[key]))

        with _make_user(handler) as c:
            _CALLS[key](c)

        method, raw_path = _ROUTES[key]
        assert len(captured) == 1
        assert captured[0].method == method
        assert captured[0].url.raw_path == raw_path
        if key in _BODIES:
            assert json.loads(captured[0].content) == _BODIES[key]
        else:
            assert captured[0].content == b""

    def test_every_public_method_is_registered(self) -> None:
        # Member binding: a public method added to ``ModelsAPI`` has to be
        # registered in ``_ROUTES``, and so is covered by every table-driven
        # test here. ``AgentsAPI`` methods are bound in ``test_skills.py``.
        assert {m for (ns, m) in _ROUTES if ns == "models"} == {
            n for n in vars(ModelsAPI) if not n.startswith("_")
        }
        assert set(_ROUTES) == set(_CALLS) == set(_FIXTURES)

    def test_models_is_a_models_api_on_the_user_client(self) -> None:
        with _make_user(lambda req: httpx.Response(200, json=[])) as c:
            assert isinstance(c.models, ModelsAPI)

    def test_get_returns_every_field_with_the_two_renames(self) -> None:
        wire = response("agent_detail")
        # A server field the list (and so ``AgentDetail``) lacks fails here.
        assert set(wire) == set(_AGENT_WIRE_FIELDS)
        # Guard: the fixture exercises non-empty and null values.
        assert wire["unresolved_models"]
        assert wire["models"]["general"] is not None
        assert None in wire["models"].values()
        assert wire["team_id"] is not None
        with _make_user(lambda req: httpx.Response(200, json=wire)) as c:
            detail = c.agents.get(wire["id"])
        assert isinstance(detail, AgentDetail)
        assert detail.agent_id == wire["id"]
        assert detail.owner_user_id == wire["user_id"]
        for name in _AGENT_WIRE_FIELDS:
            if name in ("id", "user_id"):
                continue
            assert getattr(detail, name) == wire[name], name
        assert not hasattr(detail, "id")
        assert not hasattr(detail, "user_id")
        # Every wire field is carried over: nothing is silently dropped.
        assert set(vars(detail)) == (set(_AGENT_WIRE_FIELDS) - {"id", "user_id"}) | {
            "agent_id",
            "owner_user_id",
        }

    def test_list_returns_every_field_of_every_model(self) -> None:
        wire = response("model_list")  # type: ignore[arg-type]
        assert isinstance(wire, list)
        assert len(wire) >= 2
        assert {m["category"] for m in wire} > {"llm"}
        with _make_user(lambda req: httpx.Response(200, json=wire)) as c:
            models = c.models.list()
        assert [type(m) for m in models] == [ModelInfo] * len(wire)
        for model, item in zip(models, wire, strict=True):
            # A server field ``ModelInfo`` lacks fails here, and so does an
            # attribute that is not a wire field.
            assert set(item) == set(_MODEL_FIELDS)
            assert set(vars(model)) == set(_MODEL_FIELDS)
            for name in _MODEL_FIELDS:
                assert getattr(model, name) == item[name], name

    def test_list_keeps_the_server_order_and_accepts_empty(self) -> None:
        wire = list(reversed(response("model_list")))  # type: ignore[call-overload]
        with _make_user(lambda req: httpx.Response(200, json=wire)) as c:
            assert [m.id for m in c.models.list()] == [m["id"] for m in wire]
        with _make_user(lambda req: httpx.Response(200, json=[])) as c:
            assert c.models.list() == []

    def test_results_are_frozen(self) -> None:
        with _make_user(
            lambda req: httpx.Response(200, json=response("agent_detail"))
        ) as c:
            detail = c.agents.get(1)
        with pytest.raises(AttributeError):
            detail.name = "x"  # type: ignore[misc]
        with _make_user(
            lambda req: httpx.Response(200, json=response("model_list"))
        ) as c:
            model = c.models.list()[0]
        with pytest.raises(AttributeError):
            model.id = 9  # type: ignore[misc]


class TestRequestArguments:
    @pytest.mark.parametrize(
        "agent_id",
        [".", "..", "5/tools", "1", 1.0, None],
        ids=["dot", "dotdot", "slash", "digit-string", "float", "none"],
    )
    def test_non_integer_agent_id_is_refused_before_any_request(
        self, agent_id: Any
    ) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("agent_detail"))

        with _make_user(handler) as c, pytest.raises(TypeError):
            c.agents.get(agent_id)
        assert captured == []

    def test_a_bool_agent_id_counts_as_an_integer(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("agent_detail"))

        with _make_user(handler) as c:
            c.agents.get(True)
        assert [r.url.raw_path for r in captured] == [b"/v1/agents/1"]

    def test_a_large_integer_agent_id_is_one_path_segment(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("agent_detail"))

        with _make_user(handler) as c:
            c.agents.get(2**31 - 1)
        assert [r.url.raw_path for r in captured] == [b"/v1/agents/2147483647"]

    def test_list_takes_no_arguments(self) -> None:
        with _make_user(lambda req: httpx.Response(200, json=[])) as c:
            with pytest.raises(TypeError):
                c.models.list(1)  # type: ignore[call-arg]
            with pytest.raises(TypeError):
                c.models.list(agent_id=1)  # type: ignore[call-arg]


# (status, response keyword arguments, expected ``http_status``)
_UNEXPECTED_SUCCESS: dict[str, tuple[int, dict[str, Any], int | None]] = {
    "html-200": (
        200,
        {"text": "<html>proxy</html>", "headers": {"content-type": "text/html"}},
        None,
    ),
    "created-201": (201, {"json": []}, 201),
    "redirect-302": (302, {"headers": {"location": "https://elsewhere.example/"}}, 302),
    "no-content-204": (204, {}, 204),
}


class TestUnexpectedSuccessResponses:
    @pytest.mark.parametrize("key", _KEYS, ids=_IDS)
    @pytest.mark.parametrize("kind", list(_UNEXPECTED_SUCCESS))
    def test_unexpected_success_is_malformed_response(
        self, key: tuple[str, str], kind: str
    ) -> None:
        status, kwargs, expected_status = _UNEXPECTED_SUCCESS[kind]
        if status == 201 and key[0] == "agents":
            kwargs = {"json": response(_FIXTURES[key])}
        with (
            _make_user(lambda req: httpx.Response(status, **kwargs)) as c,
            pytest.raises(MalformedResponse) as exc,
        ):
            _CALLS[key](c)
        assert exc.value.http_status == expected_status


class TestAgentDetailParsing:
    @pytest.mark.parametrize(
        "body",
        [[], None, "x", 5, [response("agent_detail")]],
        ids=["empty-array", "null", "string", "number", "array-of-object"],
    )
    def test_a_body_that_is_not_an_object_is_malformed(self, body: Any) -> None:
        with (
            _make_user(lambda req: _json_response(body)) as c,
            pytest.raises(MalformedResponse, match="expected a JSON object") as exc,
        ):
            c.agents.get(7)
        assert exc.value.http_status is None

    @pytest.mark.parametrize("missing", _AGENT_WIRE_FIELDS)
    def test_a_body_missing_any_field_is_malformed(self, missing: str) -> None:
        body = {k: v for k, v in response("agent_detail").items() if k != missing}
        with (
            _make_user(lambda req: httpx.Response(200, json=body)) as c,
            pytest.raises(MalformedResponse) as exc,
        ):
            c.agents.get(7)
        assert exc.value.http_status is None
        # The message names the SDK attribute for the two renamed fields.
        expected = {"id": "agent_id", "user_id": "owner_user_id"}.get(missing, missing)
        assert expected in exc.value.message

    def test_an_empty_object_is_never_read_as_an_empty_agent(self) -> None:
        with (
            _make_user(lambda req: httpx.Response(200, json={})) as c,
            pytest.raises(MalformedResponse),
        ):
            c.agents.get(7)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("id", "seven"),
            ("user_id", None),
            ("team_id", "one"),
            ("visibility", 3),
            ("name", None),
            ("description", ["x"]),
            ("instructions", {"a": 1}),
            ("execution_mode", None),
            ("unresolved_models", "visual"),
            ("unresolved_models", [1]),
            ("knowledge_bases", "kb"),
            ("skills", None),
            ("tool_categories", {"a": 1}),
            ("suggested_prompts", "p"),
            ("prompt_context_fields", ["order_ref"]),
            ("prompt_context_fields", {"name": "x"}),
            ("logo_url", 5),
            ("status", None),
            ("published_at", 5),
            ("created_at", None),
            ("updated_at", 5),
            ("widget_enabled", "maybe"),
            ("allowed_domains", "example.com"),
            ("share_enabled", None),
            ("share_updated_at", 5),
        ],
    )
    def test_a_field_of_the_wrong_type_is_malformed(
        self, field: str, value: Any
    ) -> None:
        body = {**response("agent_detail"), field: value}
        expected = {"id": "agent_id", "user_id": "owner_user_id"}.get(field, field)
        with (
            _make_user(lambda req: httpx.Response(200, json=body)) as c,
            pytest.raises(MalformedResponse, match=expected),
        ):
            c.agents.get(7)

    @pytest.mark.parametrize("models", ["general", ["general"], 5, True])
    def test_models_must_be_an_object_or_null(self, models: Any) -> None:
        body = {**response("agent_detail"), "models": models}
        with (
            _make_user(lambda req: httpx.Response(200, json=body)) as c,
            pytest.raises(MalformedResponse, match="models"),
        ):
            c.agents.get(7)

    def test_models_values_are_kept_as_stored(self) -> None:
        # The web app can store values that are not integer ids; the agent
        # must stay readable so it can be corrected.
        stored = {"general": "5", "small_fast": None, "visual": 1.5, "x": [1]}
        body = {**response("agent_detail"), "models": stored}
        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            detail = c.agents.get(7)
        assert detail.models == stored

    def test_models_null_and_empty_object_are_kept_apart(self) -> None:
        with _make_user(
            lambda req: httpx.Response(
                200, json={**response("agent_detail"), "models": None}
            )
        ) as c:
            assert c.agents.get(7).models is None
        with _make_user(
            lambda req: httpx.Response(
                200, json={**response("agent_detail"), "models": {}}
            )
        ) as c:
            assert c.agents.get(7).models == {}

    def test_the_sdk_attribute_names_on_the_wire_do_not_fill_a_missing_field(
        self,
    ) -> None:
        wire = response("agent_detail")
        body = {k: v for k, v in wire.items() if k not in ("id", "user_id")}
        body["agent_id"] = wire["id"]
        body["owner_user_id"] = wire["user_id"]
        with (
            _make_user(lambda req: httpx.Response(200, json=body)) as c,
            pytest.raises(MalformedResponse, match="agent_id"),
        ):
            c.agents.get(7)

    def test_extra_fields_are_ignored(self) -> None:
        body = {**response("agent_detail"), "added_later": {"a": 1}}
        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            detail = c.agents.get(7)
        assert detail.name == response("agent_detail")["name"]
        assert not hasattr(detail, "added_later")

    def test_unknown_status_and_visibility_values_pass_through(self) -> None:
        body = {**response("agent_detail"), "status": "retired", "visibility": "v9"}
        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            detail = c.agents.get(7)
        assert (detail.status, detail.visibility) == ("retired", "v9")

    def test_the_other_value_of_every_variable_field_is_read_back(self) -> None:
        # The fixture holds only one value for each of these fields; a parser
        # that fixed or narrowed any of them would pass without this.
        flipped: dict[str, Any] = {
            "description": None,
            "instructions": None,
            "logo_url": "https://example.test/logo.png",
            "published_at": "2026-05-21T10:00:00.000000",
            "share_updated_at": "2026-05-22T10:00:00.000000",
            "widget_enabled": False,
            "share_enabled": True,
            "unresolved_models": [],
            "team_id": None,
            "models": None,
        }
        wire = {**response("agent_detail"), **flipped}
        with _make_user(lambda req: httpx.Response(200, json=wire)) as c:
            detail = c.agents.get(7)
        for name, value in flipped.items():
            assert getattr(detail, name) == value, name
        assert detail.widget_enabled is False
        assert detail.share_enabled is True

    def test_a_personal_agent_reads_with_no_team(self) -> None:
        body = {**response("agent_detail"), "team_id": None}
        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            assert c.agents.get(7).team_id is None


class TestModelListParsing:
    @pytest.mark.parametrize(
        "body",
        [{}, None, "x", 5, {"models": []}],
        ids=["object", "null", "string", "number", "wrapped-array"],
    )
    def test_a_body_that_is_not_an_array_is_malformed(self, body: Any) -> None:
        with (
            _make_user(lambda req: _json_response(body)) as c,
            pytest.raises(MalformedResponse, match="expected a JSON array") as exc,
        ):
            c.models.list()
        assert exc.value.http_status is None

    @pytest.mark.parametrize("missing", _MODEL_FIELDS)
    def test_an_entry_missing_any_field_is_malformed(self, missing: str) -> None:
        wire = response("model_list")
        bad = {k: v for k, v in wire[0].items() if k != missing}  # type: ignore[attr-defined]
        with (
            _make_user(lambda req: httpx.Response(200, json=[bad])) as c,
            pytest.raises(MalformedResponse, match=missing),
        ):
            c.models.list()

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("id", "three"),
            ("model_id", None),
            ("model_name", 5),
            ("model_provider", None),
            ("category", ["llm"]),
            ("abilities", "chat"),
            ("abilities", [1]),
            ("is_platform", {"a": 1}),
            ("owned_by_you", None),
        ],
    )
    def test_an_entry_field_of_the_wrong_type_is_malformed(
        self, field: str, value: Any
    ) -> None:
        wire = response("model_list")
        bad = {**wire[0], field: value}  # type: ignore[dict-item]
        with (
            _make_user(lambda req: httpx.Response(200, json=[bad])) as c,
            pytest.raises(MalformedResponse, match=field),
        ):
            c.models.list()

    def test_the_other_value_of_every_flag_is_read_back(self) -> None:
        wire = response("model_list")
        # Guard: the fixture alone never exercises the other values.
        assert {m["is_platform"] for m in wire} == {False}  # type: ignore[attr-defined]
        flipped = [
            {**wire[0], "is_platform": True, "owned_by_you": False},  # type: ignore[dict-item]
            {**wire[0], "is_platform": False, "owned_by_you": True},  # type: ignore[dict-item]
        ]
        with _make_user(lambda req: httpx.Response(200, json=flipped)) as c:
            first, second = c.models.list()
        assert (first.is_platform, first.owned_by_you) == (True, False)
        assert (second.is_platform, second.owned_by_you) == (False, True)

    def test_one_bad_entry_fails_the_whole_call(self) -> None:
        wire = response("model_list")
        body = [*wire, "not-an-object"]  # type: ignore[misc]
        with (
            _make_user(lambda req: httpx.Response(200, json=body)) as c,
            pytest.raises(MalformedResponse),
        ):
            c.models.list()

    def test_extra_fields_and_unknown_categories_are_accepted(self) -> None:
        wire = response("model_list")
        body = [{**wire[0], "added_later": 1, "category": "rerank"}]  # type: ignore[dict-item]
        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            (model,) = c.models.list()
        assert model.category == "rerank"


# (call key, status, body, expected class)
_ERROR_CASES: list[tuple[tuple[str, str], int, dict[str, Any], type[XAgentError]]] = [
    (("agents", "get"), 401, error_envelope("invalid_api_key"), InvalidAPIKey),
    (("models", "list"), 401, error_envelope("invalid_api_key"), InvalidAPIKey),
    (("agents", "get"), 404, error_envelope("agent_not_found"), AgentNotFound),
    (("agents", "get"), 422, error_envelope("validation_422"), InvalidInput),
    (("agents", "get"), 500, error_envelope("internal_error"), InternalError),
    (("models", "list"), 500, error_envelope("internal_error"), InternalError),
]


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("key", "status", "body", "expected"),
        _ERROR_CASES,
        ids=[f"{c[0][1]}-{c[1]}-{c[3].__name__}" for c in _ERROR_CASES],
    )
    def test_error_mapping(
        self,
        key: tuple[str, str],
        status: int,
        body: dict[str, Any],
        expected: type[XAgentError],
    ) -> None:
        # Deleting a row of the table breaks this count.
        assert len(_ERROR_CASES) == 6
        with (
            _make_user(lambda req: httpx.Response(status, json=body)) as c,
            pytest.raises(XAgentError) as exc,
        ):
            _CALLS[key](c)
        assert type(exc.value) is expected
        assert exc.value.http_status == status

    @pytest.mark.parametrize("key", _KEYS, ids=_IDS)
    @pytest.mark.parametrize(
        ("status", "kwargs"),
        [
            (503, {"json": {"detail": "Startup file storage sync failed"}}),
            (404, {"json": {"detail": "Not Found"}}),
            (502, {"text": "<html>bad gateway</html>"}),
        ],
        ids=["startup-503", "no-such-route-404", "proxy-502"],
    )
    def test_responses_without_the_envelope_are_internal_errors(
        self, key: tuple[str, str], status: int, kwargs: dict[str, Any]
    ) -> None:
        with (
            _make_user(lambda req: httpx.Response(status, **kwargs)) as c,
            pytest.raises(XAgentError) as exc,
        ):
            _CALLS[key](c)
        assert type(exc.value) is InternalError
        assert exc.value.http_status == status
        assert exc.value.details is None
