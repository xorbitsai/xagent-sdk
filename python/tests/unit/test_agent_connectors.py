"""Tests for ``UserClient.agents.get_connectors()`` and ``replace_connectors()``.

Each block below pins one invariant of the two methods: how ``agent_id``
becomes a path, which arguments are refused before any request, how success
and failure bodies are parsed, and which error class each failure maps to.
Wire bodies come from ``shared/fixtures/v1/`` so the tests consume the same
JSON other language clients will use.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from xagent_sdk import (
    AgentConnectors,
    AgentNotFound,
    AgentToolsUnconfigured,
    ConnectorNotShared,
    InternalError,
    InvalidAPIKey,
    InvalidInput,
    MalformedResponse,
    UserClient,
    XAgentError,
)
from xagent_sdk.errors import _CODE_MAP

from ._fixtures import error_envelope, response

_Handler = Callable[[httpx.Request], httpx.Response]
_Call = Callable[[UserClient], object]


def _make_user(handler: _Handler) -> UserClient:
    return UserClient(
        personal_key="xag_personal_p_s",
        base_url="https://test.example",
        transport=httpx.MockTransport(handler),
    )


# One canonical call per method; ``_FIXTURE`` is the body a real server
# answers both with.
_CALLS: dict[tuple[str, str], _Call] = {
    ("agents", "get_connectors"): lambda u: u.agents.get_connectors(7),
    ("agents", "replace_connectors"): lambda u: u.agents.replace_connectors(
        7, ["crm", "ops"], all_connectors=False
    ),
}
_FIXTURE = "agent_connectors"

# HTTP method and raw request path of each canonical call.
_ROUTES: dict[tuple[str, str], tuple[str, bytes]] = {
    ("agents", "get_connectors"): ("GET", b"/v1/agents/7/connectors"),
    ("agents", "replace_connectors"): ("PUT", b"/v1/agents/7/connectors"),
}
_EXPECTED_BODIES: dict[tuple[str, str], dict[str, Any] | None] = {
    ("agents", "get_connectors"): None,
    ("agents", "replace_connectors"): {
        "connectors": ["crm", "ops"],
        "all_connectors": False,
    },
}
_CALL_BY_METHOD: dict[str, _Call] = {m: call for (_, m), call in _CALLS.items()}
assert len(_CALL_BY_METHOD) == len(_CALLS)

_BAD_IDS: list[Any] = [".", "..", "5/tools", "1", 1.0, None]
_BAD_ID_LABELS = ["dot", "dotdot", "slash", "digit-string", "float", "none"]

_FIELDS = ["agent_id", "connectors", "all_connectors", "unresolved"]


def _id_calls(agent_id: Any) -> dict[str, _Call]:
    return {
        "get_connectors": lambda u: u.agents.get_connectors(agent_id),
        "replace_connectors": lambda u: u.agents.replace_connectors(
            agent_id, ["crm"], all_connectors=False
        ),
    }


class TestRoutes:
    @pytest.mark.parametrize(("namespace", "method"), list(_ROUTES))
    def test_methods_send_expected_method_path_and_body(
        self, namespace: str, method: str
    ) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response(_FIXTURE))

        with _make_user(handler) as c:
            _CALLS[(namespace, method)](c)

        http_method, raw_path = _ROUTES[(namespace, method)]
        assert len(captured) == 1
        assert captured[0].method == http_method
        assert captured[0].url.raw_path == raw_path
        expected = _EXPECTED_BODIES[(namespace, method)]
        if expected is None:
            assert captured[0].content == b""
        else:
            assert json.loads(captured[0].content) == expected

    def test_read_and_write_return_every_wire_field(self) -> None:
        fixture = response(_FIXTURE)
        # Guard: the fixture must exercise a non-empty ``unresolved``.
        assert fixture["unresolved"]
        with _make_user(lambda req: httpx.Response(200, json=fixture)) as c:
            read = c.agents.get_connectors(1)
            written = c.agents.replace_connectors(
                1, ["crm", "ops"], all_connectors=False
            )
        for result in (read, written):
            assert isinstance(result, AgentConnectors)
            assert result.agent_id == fixture["agent_id"]
            assert result.connectors == fixture["connectors"]
            assert result.all_connectors is fixture["all_connectors"]
            assert result.unresolved == fixture["unresolved"]

    def test_the_result_is_frozen(self) -> None:
        with _make_user(lambda req: httpx.Response(200, json=response(_FIXTURE))) as c:
            result = c.agents.get_connectors(1)
        with pytest.raises(AttributeError):
            result.connectors = []  # type: ignore[misc]


class TestRequestArguments:
    @pytest.mark.parametrize("agent_id", _BAD_IDS, ids=_BAD_ID_LABELS)
    @pytest.mark.parametrize("method", ["get_connectors", "replace_connectors"])
    def test_non_integer_agent_id_is_refused_before_any_request(
        self, method: str, agent_id: Any
    ) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response(_FIXTURE))

        with _make_user(handler) as c, pytest.raises(TypeError):
            _id_calls(agent_id)[method](c)
        assert captured == []

    def test_a_bool_agent_id_counts_as_an_integer(self) -> None:
        # Documented: ``True`` and ``False`` are read as 1 and 0.
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response(_FIXTURE))

        with _make_user(handler) as c:
            c.agents.get_connectors(True)
        assert [r.url.raw_path for r in captured] == [b"/v1/agents/1/connectors"]

    def test_a_large_integer_agent_id_is_one_path_segment(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response(_FIXTURE))

        with _make_user(handler) as c:
            c.agents.get_connectors(2**31 - 1)
        assert [r.url.raw_path for r in captured] == [
            b"/v1/agents/2147483647/connectors"
        ]

    @pytest.mark.parametrize("names", ["crm", ""], ids=["name", "empty"])
    def test_a_single_string_is_refused_before_any_request(self, names: str) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response(_FIXTURE))

        with _make_user(handler) as c, pytest.raises(TypeError, match="single string"):
            c.agents.replace_connectors(7, names, all_connectors=False)
        assert captured == []

    def test_a_tuple_is_sent_as_the_same_bytes_as_a_list(self) -> None:
        sent: list[bytes] = []

        def handler(req: httpx.Request) -> httpx.Response:
            sent.append(req.content)
            return httpx.Response(200, json=response(_FIXTURE))

        with _make_user(handler) as c:
            c.agents.replace_connectors(7, ["crm", "ops"], all_connectors=True)
            c.agents.replace_connectors(7, ("crm", "ops"), all_connectors=True)
        assert len(sent) == 2
        assert sent[0] == sent[1]

    def test_the_names_are_sent_as_given_without_client_side_checks(self) -> None:
        # The server is the only authority on count, length and blanks.
        names = ["", "  ", "CRM", "crm", "crm", "x" * 101]
        sent: list[dict[str, Any]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            sent.append(json.loads(req.content))
            return httpx.Response(200, json=response(_FIXTURE))

        with _make_user(handler) as c:
            c.agents.replace_connectors(7, names, all_connectors=False)
        assert sent == [{"connectors": names, "all_connectors": False}]

    @pytest.mark.parametrize("flag", [True, False])
    def test_all_connectors_is_sent_exactly_as_given(self, flag: bool) -> None:
        sent: list[dict[str, Any]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            sent.append(json.loads(req.content))
            return httpx.Response(200, json=response(_FIXTURE))

        with _make_user(handler) as c:
            c.agents.replace_connectors(7, [], all_connectors=flag)
        assert sent == [{"connectors": [], "all_connectors": flag}]

    def test_all_connectors_cannot_be_left_out_or_passed_by_position(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response(_FIXTURE))

        with _make_user(handler) as c:
            with pytest.raises(TypeError):
                c.agents.replace_connectors(7, ["crm"])  # type: ignore[call-arg]
            with pytest.raises(TypeError):
                c.agents.replace_connectors(7, ["crm"], False)  # type: ignore[call-arg]
        assert captured == []


# (status, response keyword arguments, expected ``http_status``)
_UNEXPECTED_SUCCESS: dict[str, tuple[int, dict[str, Any], int | None]] = {
    "html-200": (
        200,
        {"text": "<html>proxy</html>", "headers": {"content-type": "text/html"}},
        None,
    ),
    "created-201": (201, {"json": response(_FIXTURE)}, 201),
    "redirect-302": (302, {"headers": {"location": "https://elsewhere.example/"}}, 302),
    "no-content-204": (204, {}, 204),
}


class TestUnexpectedSuccessResponses:
    @pytest.mark.parametrize("method", ["get_connectors", "replace_connectors"])
    @pytest.mark.parametrize("kind", list(_UNEXPECTED_SUCCESS))
    def test_unexpected_success_is_malformed_response(
        self, method: str, kind: str
    ) -> None:
        status, kwargs, expected_status = _UNEXPECTED_SUCCESS[kind]
        with (
            _make_user(lambda req: httpx.Response(status, **kwargs)) as c,
            pytest.raises(MalformedResponse) as exc,
        ):
            _CALL_BY_METHOD[method](c)
        assert exc.value.http_status == expected_status


class TestParsing:
    @pytest.mark.parametrize("method", ["get_connectors", "replace_connectors"])
    @pytest.mark.parametrize(
        "body",
        [[], None, "x", 5, [{"agent_id": 7}]],
        ids=["array", "null", "string", "number", "array-of-object"],
    )
    def test_a_body_that_is_not_an_object_is_malformed(
        self, method: str, body: Any
    ) -> None:
        raw = json.dumps(body).encode()
        with (
            _make_user(
                lambda req: httpx.Response(
                    200, content=raw, headers={"content-type": "application/json"}
                )
            ) as c,
            pytest.raises(MalformedResponse, match="expected a JSON object") as exc,
        ):
            _CALL_BY_METHOD[method](c)
        assert exc.value.http_status is None

    @pytest.mark.parametrize("method", ["get_connectors", "replace_connectors"])
    @pytest.mark.parametrize("missing", _FIELDS)
    def test_a_body_missing_any_field_is_malformed(
        self, method: str, missing: str
    ) -> None:
        body = {k: v for k, v in response(_FIXTURE).items() if k != missing}
        with (
            _make_user(lambda req: httpx.Response(200, json=body)) as c,
            pytest.raises(MalformedResponse, match=missing) as exc,
        ):
            _CALL_BY_METHOD[method](c)
        assert exc.value.http_status is None

    @pytest.mark.parametrize("method", ["get_connectors", "replace_connectors"])
    def test_an_empty_object_is_never_read_as_an_empty_selection(
        self, method: str
    ) -> None:
        with (
            _make_user(lambda req: httpx.Response(200, json={})) as c,
            pytest.raises(MalformedResponse),
        ):
            _CALL_BY_METHOD[method](c)

    @pytest.mark.parametrize("method", ["get_connectors", "replace_connectors"])
    def test_a_field_of_the_wrong_type_is_malformed(self, method: str) -> None:
        body = {**response(_FIXTURE), "connectors": "crm"}
        with (
            _make_user(lambda req: httpx.Response(200, json=body)) as c,
            pytest.raises(MalformedResponse, match="connectors"),
        ):
            _CALL_BY_METHOD[method](c)

    @pytest.mark.parametrize("method", ["get_connectors", "replace_connectors"])
    def test_extra_fields_are_ignored(self, method: str) -> None:
        body = {**response(_FIXTURE), "added_later": {"a": 1}}
        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            result = _CALL_BY_METHOD[method](c)
        assert isinstance(result, AgentConnectors)
        assert result.connectors == response(_FIXTURE)["connectors"]

    def test_read_and_write_failures_differ_in_what_they_claim(self) -> None:
        # A failed read says nothing about the server accepting anything; a
        # failed write body comes after the server accepted the write.
        with _make_user(lambda req: httpx.Response(200, json={})) as c:
            with pytest.raises(MalformedResponse) as read:
                c.agents.get_connectors(7)
            with pytest.raises(MalformedResponse) as write:
                c.agents.replace_connectors(7, [], all_connectors=False)
        assert "the server accepted the request" not in read.value.message
        assert "the server accepted the request" in write.value.message


def _envelope(code: str, message: str = "m") -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


# (method, status, body, expected class)
_ERROR_CASES: list[tuple[str, int, dict[str, Any], type[XAgentError]]] = [
    ("get_connectors", 401, error_envelope("invalid_api_key"), InvalidAPIKey),
    ("replace_connectors", 401, error_envelope("invalid_api_key"), InvalidAPIKey),
    ("get_connectors", 404, error_envelope("agent_not_found"), AgentNotFound),
    ("replace_connectors", 404, error_envelope("agent_not_found"), AgentNotFound),
    ("get_connectors", 422, error_envelope("validation_422"), InvalidInput),
    ("replace_connectors", 422, error_envelope("validation_422"), InvalidInput),
    (
        "replace_connectors",
        400,
        _envelope(
            "invalid_input",
            "These connectors are not visible to the key holder: 'nobody-has-this'",
        ),
        InvalidInput,
    ),
    ("get_connectors", 500, error_envelope("internal_error"), InternalError),
    ("replace_connectors", 500, error_envelope("internal_error"), InternalError),
    (
        "replace_connectors",
        400,
        error_envelope("connector_not_shared"),
        ConnectorNotShared,
    ),
    (
        "replace_connectors",
        409,
        error_envelope("agent_tools_unconfigured"),
        AgentToolsUnconfigured,
    ),
]


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("method", "status", "body", "expected"),
        _ERROR_CASES,
        ids=[f"{c[0]}-{c[1]}-{c[3].__name__}" for c in _ERROR_CASES],
    )
    def test_error_mapping(
        self,
        method: str,
        status: int,
        body: dict[str, Any],
        expected: type[XAgentError],
    ) -> None:
        # Deleting a row of the table breaks this count.
        assert len(_ERROR_CASES) == 11
        with (
            _make_user(lambda req: httpx.Response(status, json=body)) as c,
            pytest.raises(XAgentError) as exc,
        ):
            _CALL_BY_METHOD[method](c)
        assert type(exc.value) is expected
        assert exc.value.http_status == status

    @pytest.mark.parametrize("method", ["get_connectors", "replace_connectors"])
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
        self, method: str, status: int, kwargs: dict[str, Any]
    ) -> None:
        with (
            _make_user(lambda req: httpx.Response(status, **kwargs)) as c,
            pytest.raises(XAgentError) as exc,
        ):
            _CALL_BY_METHOD[method](c)
        assert type(exc.value) is InternalError
        assert exc.value.http_status == status
        assert exc.value.details is None

    def test_connector_not_shared_passes_details_through(self) -> None:
        body = error_envelope("connector_not_shared")
        with (
            _make_user(lambda req: httpx.Response(400, json=body)) as c,
            pytest.raises(ConnectorNotShared) as exc,
        ):
            c.agents.replace_connectors(7, ["x"], all_connectors=False)
        assert exc.value.code == "connector_not_shared"
        assert exc.value.message == body["error"]["message"]
        assert exc.value.details == {
            "connectors": [
                {"name": "x", "reason": "not_shared"},
                {"name": "y", "reason": "unresolved"},
            ]
        }

    def test_connector_not_shared_keeps_unknown_reasons_and_empty_names(self) -> None:
        # The reason list can grow and a name can be empty; the SDK does not
        # interpret either.
        sent = {
            "connectors": [
                {"name": "", "reason": "something_new"},
                {"name": "y", "reason": "disabled"},
            ]
        }
        body = {
            "error": {
                "code": "connector_not_shared",
                "message": "m",
                "details": sent,
            }
        }
        with (
            _make_user(lambda req: httpx.Response(400, json=body)) as c,
            pytest.raises(ConnectorNotShared) as exc,
        ):
            c.agents.replace_connectors(7, ["y"], all_connectors=True)
        assert exc.value.details == sent

    def test_agent_tools_unconfigured_has_no_details(self) -> None:
        body = error_envelope("agent_tools_unconfigured")
        with (
            _make_user(lambda req: httpx.Response(409, json=body)) as c,
            pytest.raises(AgentToolsUnconfigured) as exc,
        ):
            c.agents.replace_connectors(7, ["x"], all_connectors=False)
        assert exc.value.http_status == 409
        assert exc.value.details is None

    def test_get_never_raises_agent_tools_unconfigured_for_the_default_shape(
        self,
    ) -> None:
        # An agent on the default tool set reads as ``connectors=[]`` with
        # ``all_connectors=True``; writing that selection back to it is
        # refused with a 409.
        sent: list[dict[str, Any]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "agent_id": 7,
                        "connectors": [],
                        "all_connectors": True,
                        "unresolved": [],
                    },
                )
            sent.append(json.loads(req.content))
            return httpx.Response(409, json=error_envelope("agent_tools_unconfigured"))

        with _make_user(handler) as c:
            current = c.agents.get_connectors(7)
            assert current.connectors == []
            assert current.all_connectors is True
            with pytest.raises(AgentToolsUnconfigured):
                c.agents.replace_connectors(
                    7, current.connectors, all_connectors=current.all_connectors
                )
        assert sent == [{"connectors": [], "all_connectors": True}]


class TestCodeTable:
    def test_the_two_codes_are_mapped(self) -> None:
        assert _CODE_MAP["connector_not_shared"] is ConnectorNotShared
        assert _CODE_MAP["agent_tools_unconfigured"] is AgentToolsUnconfigured

    @pytest.mark.parametrize(
        ("fixture", "cls"),
        [
            ("connector_not_shared", ConnectorNotShared),
            ("agent_tools_unconfigured", AgentToolsUnconfigured),
        ],
    )
    def test_each_error_fixture_carries_the_code_of_its_class(
        self, fixture: str, cls: type[XAgentError]
    ) -> None:
        errors_dir = Path(__file__).resolve().parents[3] / "shared/fixtures/v1/errors"
        assert (errors_dir / f"{fixture}.json").is_file()
        code = error_envelope(fixture)["error"]["code"]
        assert code == fixture
        assert _CODE_MAP[code] is cls
