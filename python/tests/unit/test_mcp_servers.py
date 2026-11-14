"""Tests for ``UserClient.mcp_servers`` (``/v1/mcp-servers``).

Wire bodies come from ``shared/fixtures/v1/`` so the tests consume the
same canonical JSON a real server sends.
"""

import copy
import dataclasses
import json
from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest
from pydantic import ValidationError

from xagent_sdk import (
    AgentClient,
    ConnectorNotFound,
    InternalError,
    MalformedResponse,
    McpServer,
    UserClient,
)
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
}
_ROUTES: dict[str, tuple[str, bytes]] = {
    "list": ("GET", b"/v1/mcp-servers"),
    "get": ("GET", b"/v1/mcp-servers/7"),
}
_EXPECTED_BODIES: dict[str, object] = {
    "list": None,
    "get": None,
}
_OK_FIXTURES: dict[str, str] = {
    "list": "mcp_servers_list",
    "get": "mcp_server_detail",
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

    def test_connector_not_found_keeps_its_status(self) -> None:
        body = error_envelope("connector_not_found")
        with (
            _make_user(lambda req: httpx.Response(404, json=body)) as c,
            pytest.raises(ConnectorNotFound) as exc,
        ):
            c.mcp_servers.get(7)
        assert exc.value.code == "connector_not_found"
        assert exc.value.http_status == 404


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


class TestServerIdPath:
    @pytest.mark.parametrize("server_id", ["5/tools", "1", 1.0, None])
    def test_an_id_that_is_not_an_integer_is_refused_before_sending(
        self, server_id: object
    ) -> None:
        calls: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req)
            return httpx.Response(200, json=_detail())

        with _make_user(handler) as c, pytest.raises(TypeError):
            c.mcp_servers.get(server_id)  # type: ignore[arg-type]
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
