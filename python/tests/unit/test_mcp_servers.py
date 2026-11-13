"""Tests for ``UserClient.mcp_servers`` (``/v1/mcp-servers``).

Wire bodies come from ``shared/fixtures/v1/`` so the tests consume the
same canonical JSON a real server sends.
"""

import copy
import dataclasses
import inspect
import json
import math
from collections.abc import Callable
from types import MappingProxyType
from typing import Any, cast

import httpx
import pytest
from pydantic import ValidationError

from xagent_sdk import (
    AgentClient,
    ConnectorForbidden,
    ConnectorNameConflict,
    ConnectorNotEditable,
    ConnectorNotFound,
    ConnectorToolsUnavailable,
    ConnectorUnavailable,
    InternalError,
    InvalidInput,
    MalformedResponse,
    McpOAuthAuthorizationFailed,
    McpServer,
    McpServerTools,
    RateLimited,
    TransportNotSupported,
    UserClient,
    XAgentError,
)
from xagent_sdk._http import _DEFAULT_CONNECT_TIMEOUT
from xagent_sdk._mcp_servers import McpServersAPI
from xagent_sdk.cloud import WorkspaceClient

from ._fixtures import error_envelope, response

_Handler = Callable[[httpx.Request], httpx.Response]
_Call = Callable[[UserClient], object]


def _make_user(handler: _Handler) -> UserClient:
    return UserClient(
        personal_key="xag_personal_p_s",
        base_url="https://test.example",
        transport=httpx.MockTransport(handler),
    )


def _detail() -> dict[str, Any]:
    return copy.deepcopy(response("mcp_server_detail"))


# One canonical call per method, the request it must send, and the fixture
# a real server answers it with. Shared by the route, parsing and
# unexpected-response tests below.
_CALLS: dict[str, _Call] = {
    "list": lambda u: u.mcp_servers.list(),
    "get": lambda u: u.mcp_servers.get(7),
    "list_tools": lambda u: u.mcp_servers.list_tools(7),
    "create": lambda u: u.mcp_servers.create(
        name="n", transport="streamable_http", url="https://mcp.example.com/mcp"
    ),
    "update": lambda u: u.mcp_servers.update(7, description="d"),
    "delete": lambda u: u.mcp_servers.delete(7),
    "set_enabled": lambda u: u.mcp_servers.set_enabled(7, False),
}
_ROUTES: dict[str, tuple[str, bytes]] = {
    "list": ("GET", b"/v1/mcp-servers"),
    "get": ("GET", b"/v1/mcp-servers/7"),
    "list_tools": ("GET", b"/v1/mcp-servers/7/tools"),
    "create": ("POST", b"/v1/mcp-servers"),
    "update": ("PATCH", b"/v1/mcp-servers/7"),
    "delete": ("DELETE", b"/v1/mcp-servers/7"),
    "set_enabled": ("PUT", b"/v1/mcp-servers/7/enabled"),
}
_EXPECTED_BODIES: dict[str, object] = {
    "list": None,
    "get": None,
    "list_tools": None,
    "create": {
        "name": "n",
        "transport": "streamable_http",
        "url": "https://mcp.example.com/mcp",
        "allow_delegated_authorization": False,
        "enabled": True,
    },
    "update": {"description": "d"},
    "delete": None,
    "set_enabled": {"enabled": False},
}
_OK_FIXTURES: dict[str, str] = {
    "list": "mcp_servers_list",
    "get": "mcp_server_detail",
    "list_tools": "mcp_server_tools",
    "create": "mcp_server_write",
    "update": "mcp_server_write",
    "delete": "mcp_server_delete",
    "set_enabled": "mcp_server_write",
}
_WRITE_METHODS = ["create", "update", "delete", "set_enabled"]
# Methods that take a connector id as their first argument.
_ID_CALLS: dict[str, Callable[[UserClient, Any], object]] = {
    "get": lambda u, i: u.mcp_servers.get(i),
    "list_tools": lambda u, i: u.mcp_servers.list_tools(i),
    "update": lambda u, i: u.mcp_servers.update(i, description="d"),
    "delete": lambda u, i: cast(Any, u.mcp_servers).delete(i),
    "set_enabled": lambda u, i: u.mcp_servers.set_enabled(i, True),
}


class TestRoutes:
    @pytest.mark.parametrize("method", list(_ROUTES))
    def test_methods_send_expected_method_path_and_body(self, method: str) -> None:
        # Member binding: a public method added to the namespace has to be
        # registered here, and so is covered by every table-driven test.
        assert set(_ROUTES) == {n for n in vars(McpServersAPI) if not n.startswith("_")}
        assert set(_ROUTES) == set(_CALLS) == set(_EXPECTED_BODIES) == set(_OK_FIXTURES)

        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response(_OK_FIXTURES[method]))

        with _make_user(handler) as c:
            _CALLS[method](c)

        http_method, raw_path = _ROUTES[method]
        assert len(captured) == 1
        assert captured[0].method == http_method
        assert captured[0].url.raw_path == raw_path
        assert captured[0].headers["authorization"] == "Bearer xag_personal_p_s"
        expected_body = _EXPECTED_BODIES[method]
        if expected_body is None:
            assert captured[0].content == b""
        else:
            assert json.loads(captured[0].content) == expected_body

    def test_only_the_user_client_has_the_namespace(self) -> None:
        assert isinstance(
            UserClient(personal_key="k", base_url="https://t").mcp_servers,
            McpServersAPI,
        )
        assert not hasattr(
            AgentClient(api_key="k", base_url="https://t"), "mcp_servers"
        )
        ws = WorkspaceClient(workspace_key="k", base_url="https://t")
        assert not hasattr(ws, "mcp_servers")


# (status, response keyword arguments, expected ``http_status``): a 200
# that is not JSON is a protocol failure without a status; every other
# status is reported with itself.
_UNEXPECTED_SUCCESS: dict[str, tuple[int, dict[str, Any], int | None]] = {
    "html-200": (
        200,
        {"text": "<html>proxy</html>", "headers": {"content-type": "text/html"}},
        None,
    ),
    "redirect-302": (302, {"headers": {"location": "https://elsewhere.example/"}}, 302),
    "redirect-307-json": (307, {"json": []}, 307),
    "no-content-204": (204, {}, 204),
}


class TestUnexpectedResponses:
    @pytest.mark.parametrize("method", list(_ROUTES))
    @pytest.mark.parametrize("kind", list(_UNEXPECTED_SUCCESS))
    def test_methods_reject_unexpected_success_responses(
        self, method: str, kind: str
    ) -> None:
        status, kwargs, expected_status = _UNEXPECTED_SUCCESS[kind]
        with (
            _make_user(lambda req: httpx.Response(status, **kwargs)) as c,
            pytest.raises(MalformedResponse) as exc,
        ):
            _CALLS[method](c)
        assert exc.value.http_status == expected_status

    def test_a_server_without_the_route_is_not_connector_not_found(self) -> None:
        # The framework's own 404 carries no V1 envelope: the server has no
        # such route, which must stay distinguishable from a missing connector.
        with (
            _make_user(
                lambda req: httpx.Response(404, json={"detail": "Not Found"})
            ) as c,
            pytest.raises(InternalError) as exc,
        ):
            c.mcp_servers.get(7)
        assert type(exc.value) is InternalError
        assert exc.value.http_status == 404

    @pytest.mark.parametrize("method", _WRITE_METHODS)
    def test_a_server_with_only_the_read_routes_answers_405_as_internal_error(
        self, method: str
    ) -> None:
        # An intermediate server version has the read routes but not the
        # write ones; the framework's own 405 is not a connector error.
        with (
            _make_user(
                lambda req: httpx.Response(405, json={"detail": "Method Not Allowed"})
            ) as c,
            pytest.raises(InternalError) as exc,
        ):
            _CALLS[method](c)
        assert type(exc.value) is InternalError
        assert exc.value.http_status == 405

    def test_connector_not_found_keeps_its_status(self) -> None:
        body = error_envelope("connector_not_found")
        with (
            _make_user(lambda req: httpx.Response(404, json=body)) as c,
            pytest.raises(ConnectorNotFound) as exc,
        ):
            c.mcp_servers.get(7)
        assert exc.value.code == "connector_not_found"
        assert exc.value.http_status == 404


def _error_cases() -> list[Any]:
    # (status, fixture, expected class, expected details, response headers).
    # The 429 body and header are what a server at its tool-listing limit
    # sends: the V1 envelope without ``details`` and ``Retry-After: 3``.
    return [
        (404, error_envelope("connector_not_found"), ConnectorNotFound, None, {}),
        (
            400,
            error_envelope("transport_not_supported"),
            TransportNotSupported,
            None,
            {},
        ),
        (
            409,
            error_envelope("connector_unavailable"),
            ConnectorUnavailable,
            {"reason": "disabled"},
            {},
        ),
        (
            409,
            error_envelope("mcp_oauth_authorization_failed"),
            McpOAuthAuthorizationFailed,
            {"reason": "authorization_required"},
            {},
        ),
        (
            502,
            error_envelope("connector_tools_unavailable"),
            ConnectorToolsUnavailable,
            {
                "reason": "no_tools",
                "failures": [{"phase": "initialize", "attempts": 3}],
            },
            {},
        ),
        (
            429,
            {
                "error": {
                    "code": "rate_limited",
                    "message": (
                        "Too many connector tool listings are running; retry shortly."
                    ),
                }
            },
            RateLimited,
            None,
            {"Retry-After": "3"},
        ),
    ]


class TestListToolsErrors:
    @pytest.mark.parametrize(
        ("status", "body", "cls", "details", "headers"),
        _error_cases(),
        ids=[c[2].__name__ for c in _error_cases()],
    )
    def test_error_maps_to_its_class_and_keeps_status_and_details(
        self,
        status: int,
        body: dict[str, Any],
        cls: type[XAgentError],
        details: dict[str, Any] | None,
        headers: dict[str, str],
    ) -> None:
        with (
            _make_user(
                lambda req: httpx.Response(status, json=body, headers=headers)
            ) as c,
            pytest.raises(cls) as exc,
        ):
            c.mcp_servers.list_tools(7)
        assert type(exc.value) is cls
        assert exc.value.http_status == status
        assert exc.value.code == body["error"]["code"]
        assert exc.value.details == details
        expected_retry = 3 if cls is RateLimited else None
        assert exc.value.retry_after == expected_retry


_WRITE_ERROR_CASES: list[Any] = [
    (
        "create",
        409,
        "connector_name_conflict",
        ConnectorNameConflict,
        {"connector_id": 1},
    ),
    ("create", 422, "validation_422", InvalidInput, None),
    (
        "update",
        403,
        "connector_forbidden",
        ConnectorForbidden,
        {"reason": "team_shared"},
    ),
    (
        "update",
        409,
        "connector_not_editable",
        ConnectorNotEditable,
        {"reason": "catalog"},
    ),
    (
        "delete",
        409,
        "connector_not_editable",
        ConnectorNotEditable,
        {"reason": "catalog"},
    ),
    (
        "set_enabled",
        403,
        "connector_forbidden",
        ConnectorForbidden,
        {"reason": "team_shared"},
    ),
]


class TestWriteErrors:
    @pytest.mark.parametrize(
        ("method", "status", "fixture", "cls", "details"),
        _WRITE_ERROR_CASES,
        ids=[f"{c[0]}-{c[1]}-{c[3].__name__}" for c in _WRITE_ERROR_CASES],
    )
    def test_error_maps_to_its_class_and_keeps_status_and_details(
        self,
        method: str,
        status: int,
        fixture: str,
        cls: type[XAgentError],
        details: dict[str, Any] | None,
    ) -> None:
        body = error_envelope(fixture)
        with (
            _make_user(lambda req: httpx.Response(status, json=body)) as c,
            pytest.raises(cls) as exc,
        ):
            _CALLS[method](c)
        assert type(exc.value) is cls
        assert exc.value.http_status == status
        assert exc.value.code == body["error"]["code"]
        assert exc.value.details == details
        assert exc.value.retry_after is None


class TestListParsing:
    def test_list_fixture_parses_in_server_order(self) -> None:
        body = cast(list[dict[str, Any]], response("mcp_servers_list"))
        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            servers = c.mcp_servers.list()
        assert [s.id for s in servers] == [item["id"] for item in body]
        assert all(isinstance(s, McpServer) for s in servers)

    def test_empty_array_is_an_empty_list(self) -> None:
        with _make_user(lambda req: httpx.Response(200, json=[])) as c:
            assert c.mcp_servers.list() == []

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"json": {}},
            {"content": b"null", "headers": {"content-type": "application/json"}},
            {"json": "x"},
        ],
        ids=["object", "null", "string"],
    )
    def test_a_body_that_is_not_an_array_is_never_an_empty_list(
        self, kwargs: dict[str, Any]
    ) -> None:
        with (
            _make_user(lambda req: httpx.Response(200, **kwargs)) as c,
            pytest.raises(MalformedResponse, match="expected a JSON array") as exc,
        ):
            c.mcp_servers.list()
        assert exc.value.http_status is None

    def test_one_bad_entry_fails_the_whole_list(self) -> None:
        body = [_detail(), {"id": 1}]
        with (
            _make_user(lambda req: httpx.Response(200, json=body)) as c,
            pytest.raises(MalformedResponse),
        ):
            c.mcp_servers.list()


class TestDetailParsing:
    @pytest.mark.parametrize("body", [[], "x"], ids=["array", "string"])
    def test_a_body_that_is_not_an_object_is_malformed(self, body: object) -> None:
        with (
            _make_user(lambda req: httpx.Response(200, json=body)) as c,
            pytest.raises(MalformedResponse, match="expected a JSON object") as exc,
        ):
            c.mcp_servers.get(7)
        assert exc.value.http_status is None

    def test_detail_fixture_matches_field_for_field(self) -> None:
        body = response("mcp_server_detail")
        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            server = c.mcp_servers.get(7)
        names = [f.name for f in dataclasses.fields(McpServer)]
        assert set(names) == set(body)
        for name in names:
            if name == "authorization":
                continue
            assert getattr(server, name) == body[name]

    @pytest.mark.parametrize(
        "field",
        ["transport", "access", "kind", "authorization.kind", "authorization.status"],
    )
    def test_unknown_values_and_extra_keys_pass_through(self, field: str) -> None:
        body = _detail()
        body["future_field"] = 1
        if body["authorization"] is None:
            body["authorization"] = {
                "kind": "mcp_oauth",
                "status": "x",
                "message": None,
            }
        target: dict[str, Any] = body
        parts = field.split(".")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = "future_value"
        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            server = c.mcp_servers.get(7)
        value: Any = server
        for part in parts:
            value = getattr(value, part)
        assert value == "future_value"

    @pytest.mark.parametrize("field", [f.name for f in dataclasses.fields(McpServer)])
    def test_every_field_is_required(self, field: str) -> None:
        assert len(dataclasses.fields(McpServer)) == 25
        body = _detail()
        del body[field]
        with (
            _make_user(lambda req: httpx.Response(200, json=body)) as c,
            pytest.raises(MalformedResponse) as exc,
        ):
            c.mcp_servers.get(7)
        cause = exc.value.__cause__
        assert isinstance(cause, ValidationError)
        assert cause.errors()[0]["loc"] == (field,)


class TestListToolsParsing:
    @pytest.mark.parametrize("body", [[], "x"], ids=["array", "string"])
    def test_a_body_that_is_not_an_object_is_malformed(self, body: object) -> None:
        with (
            _make_user(lambda req: httpx.Response(200, json=body)) as c,
            pytest.raises(MalformedResponse, match="expected a JSON object") as exc,
        ):
            c.mcp_servers.list_tools(7)
        assert exc.value.http_status is None

    def test_fixture_parses_field_for_field(self) -> None:
        body = response("mcp_server_tools")
        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            result = c.mcp_servers.list_tools(7)
        assert isinstance(result, McpServerTools)
        assert result.id == body["id"]
        assert [(t.name, t.mcp_name, t.description) for t in result.tools] == [
            (t["name"], t["mcp_name"], t["description"]) for t in body["tools"]
        ]
        assert result.failures == []

    def test_unknown_phase_and_extra_keys_pass_through(self) -> None:
        body = copy.deepcopy(response("mcp_server_tools"))
        body["future_field"] = 1
        body["tools"][0]["future_field"] = 1
        body["failures"].append(
            {"phase": "future_value", "attempts": 1, "future_field": 1}
        )
        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            result = c.mcp_servers.list_tools(7)
        assert result.failures[0].phase == "future_value"
        assert result.failures[0].attempts == 1

    @pytest.mark.parametrize("key", ["id", "tools", "failures"])
    def test_every_top_level_field_is_required(self, key: str) -> None:
        body = copy.deepcopy(response("mcp_server_tools"))
        del body[key]
        with (
            _make_user(lambda req: httpx.Response(200, json=body)) as c,
            pytest.raises(MalformedResponse),
        ):
            c.mcp_servers.list_tools(7)


def _timeout_user(
    client_timeout: float | None, seen: list[httpx.Request]
) -> UserClient:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(
            200,
            json=_detail()
            if req.url.path.endswith("/7")
            else response("mcp_server_tools"),
        )

    return UserClient(
        personal_key="xag_personal_p_s",
        base_url="https://test.example",
        timeout=client_timeout,
        transport=httpx.MockTransport(handler),
    )


class TestListToolsTimeout:
    @pytest.mark.parametrize(
        ("client_timeout", "argument", "expected_read"),
        [
            (30.0, None, 150.0),
            (300.0, None, 300.0),
            (150.0, None, 150.0),
            (None, None, None),
            (30.0, 5.0, 5.0),
            (None, 5.0, 5.0),
            (300.0, 5.0, 5.0),
        ],
        ids=[
            "client-30-default",
            "client-300-default",
            "client-150-default",
            "client-none-default",
            "client-30-explicit-5",
            "client-none-explicit-5",
            "client-300-explicit-5",
        ],
    )
    def test_this_request_gets_the_expected_timeout_and_the_client_keeps_its_own(
        self,
        client_timeout: float | None,
        argument: float | None,
        expected_read: float | None,
    ) -> None:
        seen: list[httpx.Request] = []
        with _timeout_user(client_timeout, seen) as c:
            if argument is None:
                c.mcp_servers.list_tools(7)
            else:
                c.mcp_servers.list_tools(7, timeout=argument)
            c.mcp_servers.get(7)
        tools_timeout = seen[0].extensions["timeout"]
        assert tools_timeout == {
            "connect": _DEFAULT_CONNECT_TIMEOUT,
            "read": expected_read,
            "write": expected_read,
            "pool": expected_read,
        }
        # The next call on the same client is back to the client's own value.
        assert seen[1].extensions["timeout"] == {
            "connect": _DEFAULT_CONNECT_TIMEOUT,
            "read": client_timeout,
            "write": client_timeout,
            "pool": client_timeout,
        }

    @pytest.mark.parametrize("bad", [0, 0.0, -1, -0.5, math.nan, math.inf, -math.inf])
    def test_a_timeout_that_is_not_a_positive_finite_number_is_refused(
        self, bad: float
    ) -> None:
        seen: list[httpx.Request] = []
        with (
            _timeout_user(30.0, seen) as c,
            pytest.raises(
                ValueError, match="timeout must be a finite number greater than 0"
            ),
        ):
            c.mcp_servers.list_tools(7, timeout=bad)
        assert seen == []


class TestServerIdPath:
    @pytest.mark.parametrize("method", list(_ID_CALLS))
    @pytest.mark.parametrize("server_id", ["5/tools", "1", 1.0, None])
    def test_an_id_that_is_not_an_integer_is_refused_before_sending(
        self, method: str, server_id: object
    ) -> None:
        calls: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req)
            return httpx.Response(200, json=_detail())

        with _make_user(handler) as c, pytest.raises(TypeError):
            _ID_CALLS[method](c, server_id)
        assert calls == []

    @pytest.mark.parametrize(
        ("server_id", "raw_path"),
        [(1, b"/v1/mcp-servers/1"), (2**31 - 1, b"/v1/mcp-servers/2147483647")],
    )
    def test_an_integer_id_is_one_path_segment(
        self, server_id: int, raw_path: bytes
    ) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=_detail())

        with _make_user(handler) as c:
            c.mcp_servers.get(server_id)
        assert captured[0].url.raw_path == raw_path
        assert captured[0].url.query == b""


def _capture_write(
    call: Callable[[UserClient], object], fixture: str = "mcp_server_write"
) -> httpx.Request:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json=response(fixture))

    with _make_user(handler) as c:
        call(c)
    assert len(captured) == 1
    return captured[0]


def _body(req: httpx.Request) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(req.content))


_CREATE_REQUIRED: dict[str, Any] = {
    "name": "n",
    "transport": "streamable_http",
    "url": "https://mcp.example.com/mcp",
}
_CREATE_ALWAYS_SENT = {
    **_CREATE_REQUIRED,
    "allow_delegated_authorization": False,
    "enabled": True,
}
_NULLABLE_VALUES: dict[str, Any] = {
    "description": "d",
    "headers": {"X-A": "b"},
    "auth": {"type": "bearer", "bearer_token": "t"},
    "runtime_input_schema": {"type": "object"},
    "runtime_bindings": [{"k": "v"}],
    "signed_uploads": [{"u": 1}],
}


class TestCreateBody:
    @pytest.mark.parametrize("param", list(_NULLABLE_VALUES))
    def test_a_nullable_parameter_is_sent_only_when_it_has_a_value(
        self, param: str
    ) -> None:
        none_kwargs: dict[str, Any] = {param: None}
        none_body = _body(
            _capture_write(
                lambda c: c.mcp_servers.create(**_CREATE_REQUIRED, **none_kwargs)
            )
        )
        assert param not in none_body
        assert none_body == _CREATE_ALWAYS_SENT

        value = _NULLABLE_VALUES[param]
        value_body = _body(
            _capture_write(
                lambda c: c.mcp_servers.create(**_CREATE_REQUIRED, **{param: value})
            )
        )
        assert value_body[param] == value
        assert {k: v for k, v in value_body.items() if k != param} == (
            _CREATE_ALWAYS_SENT
        )

    def test_the_always_sent_keys_are_present_with_non_default_values(self) -> None:
        body = _body(
            _capture_write(
                lambda c: c.mcp_servers.create(
                    **_CREATE_REQUIRED,
                    allow_delegated_authorization=True,
                    enabled=False,
                )
            )
        )
        assert body == {
            **_CREATE_REQUIRED,
            "allow_delegated_authorization": True,
            "enabled": False,
        }

    def test_a_read_only_mapping_and_a_tuple_send_the_same_bytes_as_dict_and_list(
        self,
    ) -> None:
        plain = _capture_write(
            lambda c: c.mcp_servers.create(
                **_CREATE_REQUIRED,
                headers={"X-A": "b"},
                runtime_bindings=[{"k": "v"}],
            )
        )
        wrapped = _capture_write(
            lambda c: c.mcp_servers.create(
                **_CREATE_REQUIRED,
                headers=MappingProxyType({"X-A": "b"}),
                runtime_bindings=(MappingProxyType({"k": "v"}),),
            )
        )
        assert wrapped.content == plain.content
        assert _body(wrapped)["headers"] == {"X-A": "b"}
        assert _body(wrapped)["runtime_bindings"] == [{"k": "v"}]


_UPDATE_VALUES: dict[str, Any] = {
    "name": "n2",
    "transport": "sse",
    "url": "https://mcp.example.com/sse",
    "allow_delegated_authorization": True,
    **_NULLABLE_VALUES,
}


class TestUpdateBody:
    def test_the_table_covers_every_update_parameter(self) -> None:
        params = set(inspect.signature(McpServersAPI.update).parameters) - {
            "self",
            "server_id",
        }
        assert params == set(_UPDATE_VALUES)
        assert len(_UPDATE_VALUES) == 10

    @pytest.mark.parametrize("param", list(_UPDATE_VALUES))
    @pytest.mark.parametrize("state", ["omitted", "value", "none"])
    def test_only_the_parameters_that_were_passed_are_sent(
        self, param: str, state: str
    ) -> None:
        kwargs: dict[str, Any] = {}
        if state == "value":
            kwargs[param] = _UPDATE_VALUES[param]
        elif state == "none":
            kwargs[param] = None
        body = _body(_capture_write(lambda c: c.mcp_servers.update(7, **kwargs)))
        if state == "omitted":
            assert body == {}
        elif state == "value":
            assert body == {param: _UPDATE_VALUES[param]}
        else:
            assert body == {param: None}
            assert param in body

    def test_passing_nothing_sends_an_empty_object(self) -> None:
        req = _capture_write(lambda c: c.mcp_servers.update(7))
        assert req.content == b"{}"

    def test_a_read_only_mapping_and_a_tuple_send_the_same_bytes_as_dict_and_list(
        self,
    ) -> None:
        plain = _capture_write(
            lambda c: c.mcp_servers.update(
                7, headers={"X-A": "b"}, runtime_bindings=[{"k": "v"}]
            )
        )
        wrapped = _capture_write(
            lambda c: c.mcp_servers.update(
                7,
                headers=MappingProxyType({"X-A": "b"}),
                runtime_bindings=(MappingProxyType({"k": "v"}),),
            )
        )
        assert wrapped.content == plain.content
        assert _body(wrapped) == {
            "headers": {"X-A": "b"},
            "runtime_bindings": [{"k": "v"}],
        }

    def test_the_documented_header_edit_keeps_the_other_headers(self) -> None:
        stored = _detail()["headers"]
        req = _capture_write(
            lambda c: c.mcp_servers.update(
                7, headers={**(stored or {}), "X-Trace": "1"}
            )
        )
        assert _body(req) == {"headers": {**stored, "X-Trace": "1"}}


class TestSetEnabledBody:
    @pytest.mark.parametrize("enabled", [True, False])
    def test_the_boolean_is_sent_as_given(self, enabled: bool) -> None:
        req = _capture_write(lambda c: c.mcp_servers.set_enabled(7, enabled))
        assert _body(req) == {"enabled": enabled}

    def test_the_value_is_not_checked_locally(self) -> None:
        req = _capture_write(
            lambda c: c.mcp_servers.set_enabled(7, "true")  # type: ignore[arg-type]
        )
        assert _body(req) == {"enabled": "true"}


class TestWriteResponseParsing:
    @pytest.mark.parametrize("method", ["create", "update", "set_enabled"])
    def test_the_write_fixture_parses_into_a_connector(self, method: str) -> None:
        body = response("mcp_server_write")
        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            server = cast(McpServer, _CALLS[method](c))
        assert isinstance(server, McpServer)
        assert server.id == body["id"]
        assert set(body) == {f.name for f in dataclasses.fields(McpServer)}

    def test_delete_returns_none_for_the_confirmation(self) -> None:
        with _make_user(lambda req: httpx.Response(200, json={"id": 3})) as c:
            assert cast(Any, c.mcp_servers).delete(3) is None

    @pytest.mark.parametrize(
        "body",
        [{}, {"id": "3x"}, [], "ok"],
        ids=["empty-object", "non-numeric-id", "array", "string"],
    )
    def test_delete_rejects_a_body_that_is_not_the_confirmation(
        self, body: object
    ) -> None:
        with (
            _make_user(lambda req: httpx.Response(200, json=body)) as c,
            pytest.raises(MalformedResponse) as exc,
        ):
            c.mcp_servers.delete(3)
        assert exc.value.http_status is None

    @pytest.mark.parametrize("method", ["create", "update", "set_enabled"])
    def test_a_write_with_a_bad_body_says_the_server_accepted_the_request(
        self, method: str
    ) -> None:
        with (
            _make_user(lambda req: httpx.Response(200, json={})) as c,
            pytest.raises(MalformedResponse) as exc,
        ):
            _CALLS[method](c)
        assert "the server accepted the request" in exc.value.message

    def test_a_read_with_a_bad_body_does_not_say_so(self) -> None:
        with (
            _make_user(lambda req: httpx.Response(200, json={})) as c,
            pytest.raises(MalformedResponse) as exc,
        ):
            c.mcp_servers.get(7)
        assert "the server accepted the request" not in exc.value.message
