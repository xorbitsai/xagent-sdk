"""``response_format`` on ``create()``, ``append()`` and ``run()``: the value
sent, the local checks that refuse a value before any request, the
``GET /v1/features`` check and the echo check."""

import json
import math
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from xagent_sdk import (
    AgentClient,
    InternalError,
    InvalidInput,
    MalformedResponse,
    UnsupportedFeature,
    UnsupportedSchema,
    XAgentTransportError,
)
from xagent_sdk._schema_rules import RULE_TEXTS

from ._fixtures import error_envelope, response

_CORPUS: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "shared"
        / "fixtures"
        / "v1"
        / "structured_schema_corpus.json"
    ).read_text(encoding="utf-8")
)
_REJECT = {case["id"]: case for case in _CORPUS["reject"]}

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"total": {"type": "number"}},
    "required": ["total"],
    "additionalProperties": False,
}
_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {"name": "Invoice", "schema": _SCHEMA},
}

Call = Callable[..., object]
_CALLS: dict[str, Call] = {
    "create": lambda c, **kw: c.tasks.create(agent_id=7, message="hi", **kw),
    "append": lambda c, **kw: c.tasks.append(10, agent_id=7, message="hi", **kw),
    "run": lambda c, **kw: c.tasks.run(
        agent_id=7, message="hi", poll_interval=0.0, **kw
    ),
}
_WRITE_CALLS = {name: _CALLS[name] for name in ("create", "append")}


def _echo(rf: Any) -> dict[str, str]:
    if rf.get("type") == "json_schema":
        return {
            "response_format_type": "json_schema",
            "response_format_name": rf["json_schema"]["name"],
        }
    return {"response_format_type": "text", "response_format_name": ""}


class _Server:
    """A mock server that records every request.

    ``features`` answers ``GET /v1/features``; ``write`` the create and append
    POSTs; GETs of the task answer a completed snapshot.
    """

    def __init__(
        self,
        *,
        features: Callable[[], httpx.Response] | None = None,
        echo: dict[str, Any] | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._features = features or (
            lambda: httpx.Response(200, json=response("features"))
        )
        self._echo = echo

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path = req.url.path
        if path == "/v1/features":
            return self._features()
        if req.method == "POST":
            sent = json.loads(req.content).get("response_format")
            echo = self._echo if self._echo is not None else _echo(sent or {})
            base = (
                response("create_task")
                if path == "/v1/chat/tasks"
                else response("append_task")
            )
            return httpx.Response(202, json={**base, **echo})
        if path.endswith("/steps"):
            return httpx.Response(200, json={"task_id": 10, "steps": []})
        return httpx.Response(200, json=response("task_info_completed"))

    @property
    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]

    def body(self, index: int) -> Any:
        return json.loads(self.requests[index].content)


class TestSentValue:
    @pytest.mark.parametrize("name", list(_CALLS))
    def test_features_then_write_with_the_value(
        self, make_client: Callable[..., AgentClient], name: str
    ) -> None:
        server = _Server()
        with make_client(server) as c:
            _CALLS[name](c, response_format=_FORMAT)
        assert server.paths[0] == "GET /v1/features"
        assert server.paths[1].startswith("POST ")
        assert server.body(1)["response_format"] == _FORMAT
        assert list(server.body(1))[-1] == "response_format"

    @pytest.mark.parametrize("name", list(_CALLS))
    def test_without_response_format_nothing_changes(
        self, make_client: Callable[..., AgentClient], name: str
    ) -> None:
        server = _Server()
        with make_client(server) as c:
            _CALLS[name](c)
        assert "GET /v1/features" not in server.paths
        assert list(server.body(0)) == ["agent_id", "message"]

    def test_ordered_dict_and_tuples_are_sent_as_json(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        schema = OrderedDict(
            [
                ("type", "object"),
                (
                    "properties",
                    OrderedDict([("kind", {"type": "string", "enum": ("a", "b")})]),
                ),
                ("required", ("kind",)),
                ("additionalProperties", False),
            ]
        )
        value = {"type": "json_schema", "json_schema": {"name": "R", "schema": schema}}
        server = _Server()
        with make_client(server) as c:
            c.tasks.create(agent_id=7, message="hi", response_format=value)
        sent = server.body(1)["response_format"]["json_schema"]["schema"]
        assert sent["required"] == ["kind"]
        assert sent["properties"]["kind"]["enum"] == ["a", "b"]

    @pytest.mark.parametrize(
        "value",
        [
            "x",
            {"type": "x"},
            {"type": "json_schema"},
            {"type": "json_schema", "json_schema": {"name": "a\n"}},
            _REJECT["n7-integer-one"]["response_format"],
            _REJECT["n3-json-schema-with-strict-at-top"]["response_format"],
        ],
    )
    def test_values_left_to_the_server_are_sent_as_given(
        self, make_client: Callable[..., AgentClient], value: Any
    ) -> None:
        requests: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests.append(req)
            if req.url.path == "/v1/features":
                return httpx.Response(200, json=response("features"))
            return httpx.Response(422, json=error_envelope("validation_422"))

        with make_client(handler) as c, pytest.raises(InvalidInput):
            c.tasks.create(agent_id=7, message="hi", response_format=value)
        assert json.loads(requests[-1].content)["response_format"] == value

    @pytest.mark.parametrize(
        ("value", "error"),
        [({"type": {1, 2}}, TypeError), ({"type": 10**5000}, ValueError)],
    )
    def test_json_encoding_errors_propagate_before_any_request(
        self,
        make_client: Callable[..., AgentClient],
        value: Any,
        error: type[Exception],
    ) -> None:
        server = _Server()
        with make_client(server) as c, pytest.raises(error):
            c.tasks.create(agent_id=7, message="hi", response_format=value)
        assert server.requests == []


def _schema_case(case_id: str) -> tuple[str, Any, str, str]:
    case = _REJECT[case_id]
    return case_id, case["response_format"], case["rule"], case["path"]


def _format_with(schema: Any, **json_schema: Any) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": "R", **json_schema, "schema": schema},
    }


def _with_property(prop: dict[str, Any]) -> dict[str, Any]:
    return _format_with({**_SCHEMA, "properties": {"total": prop}})


# Values the SDK refuses locally: the character checks of S1 (NaN and
# Infinity cannot be written in the JSON corpus), a surrogate in the
# description, the trailing-newline name cases, the exact-type cases of the S
# rules, and a "format" keyword in a caller's own schema.
_LOCAL_REFUSALS: list[tuple[str, Any, str, str]] = [
    ("nan-const", _with_property({"type": "number", "const": math.nan}), "S1", ""),
    ("infinity", _with_property({"type": "number", "x": math.inf}), "S1", ""),
    *(
        _schema_case(i)
        for i in (
            "s1-nul-in-const",
            "s1-lone-surrogate-in-description",
            "s1-surrogate-property-name",
            "n5-trailing-newline",
            "s7-key-trailing-newline",
            "s8-ref-trailing-newline",
            "s13-name-trailing-newline",
            "s3-additional-properties-zero",
            "s3-type-empty-array",
            "s3-type-empty-object",
            "s4-integer-enum-true",
            "s4-number-const-false",
            "s3-enum-string",
            "s3-enum-object",
            "s3-defs-empty-array",
            "s3-defs-number-array",
            "s3-any-of-string",
        )
    ),
    (
        "n6-surrogate",
        _format_with(_SCHEMA, description="x\ud800"),
        "N6",
        "/json_schema/description",
    ),
    (
        "format-date-newline",
        _with_property({"type": "string", "format": "date\n"}),
        "S3",
        "/properties/total/format",
    ),
]


class TestLocalRefusal:
    @pytest.mark.parametrize("name", list(_WRITE_CALLS))
    @pytest.mark.parametrize(
        ("case_id", "value", "rule", "path"),
        _LOCAL_REFUSALS,
        ids=[case[0] for case in _LOCAL_REFUSALS],
    )
    def test_refused_before_any_request(
        self,
        make_client: Callable[..., AgentClient],
        name: str,
        case_id: str,
        value: Any,
        rule: str,
        path: str,
    ) -> None:
        server = _Server()
        with make_client(server) as c, pytest.raises(UnsupportedSchema) as exc:
            _WRITE_CALLS[name](c, response_format=value)
        assert server.requests == []
        where = path or (
            "response_format" if rule.startswith("N") else "the schema root"
        )
        assert exc.value.code == "unsupported_schema"
        assert exc.value.rule == rule
        assert exc.value.path == where
        assert exc.value.message == f"{rule} at {where}: {RULE_TEXTS[rule]}"
        assert exc.value.http_status is None


class TestFeatureCheck:
    @pytest.mark.parametrize("name", list(_CALLS))
    @pytest.mark.parametrize(
        "features",
        [
            lambda: httpx.Response(404, json={"detail": "Not Found"}),
            lambda: httpx.Response(200, json={"features": []}),
        ],
        ids=["no-route", "not-listed"],
    )
    def test_unsupported_raises_before_the_write(
        self,
        make_client: Callable[..., AgentClient],
        name: str,
        features: Callable[[], httpx.Response],
    ) -> None:
        server = _Server(features=features)
        with make_client(server) as c:
            for _ in range(2):
                with pytest.raises(UnsupportedFeature) as exc:
                    _CALLS[name](c, response_format=_FORMAT)
                assert exc.value.task_id is None
        assert server.paths == ["GET /v1/features"]

    @pytest.mark.parametrize(
        ("features", "error"),
        [
            (
                lambda: httpx.Response(500, json=error_envelope("internal_error")),
                InternalError,
            ),
            (lambda: httpx.Response(200, json={"features": "x"}), MalformedResponse),
        ],
    )
    def test_other_failures_propagate_and_are_asked_again(
        self,
        make_client: Callable[..., AgentClient],
        features: Callable[[], httpx.Response],
        error: type[Exception],
    ) -> None:
        server = _Server(features=features)
        with make_client(server) as c:
            for _ in range(2):
                with pytest.raises(error):
                    c.tasks.create(agent_id=7, message="hi", response_format=_FORMAT)
        assert server.paths == ["GET /v1/features", "GET /v1/features"]

    def test_timeout_propagates_and_is_asked_again(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        def timeout() -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        server = _Server(features=timeout)
        with make_client(server) as c:
            for _ in range(2):
                with pytest.raises(XAgentTransportError):
                    c.tasks.create(agent_id=7, message="hi", response_format=_FORMAT)
        assert server.paths == ["GET /v1/features", "GET /v1/features"]

    def test_answer_is_asked_once(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        server = _Server()
        with make_client(server) as c:
            c.tasks.create(agent_id=7, message="hi", response_format=_FORMAT)
            c.tasks.append(10, agent_id=7, message="hi", response_format=_FORMAT)
        assert server.paths.count("GET /v1/features") == 1


class TestEcho:
    @pytest.mark.parametrize("name", list(_WRITE_CALLS))
    @pytest.mark.parametrize(
        "echo",
        [
            {},
            {"response_format_type": "json_schema"},
            {"response_format_type": "text", "response_format_name": ""},
            {"response_format_type": "json_schema", "response_format_name": "Other"},
        ],
        ids=["no-keys", "no-name", "type-differs", "name-differs"],
    )
    def test_echo_that_does_not_match_raises_with_task_id(
        self,
        make_client: Callable[..., AgentClient],
        name: str,
        echo: dict[str, Any],
    ) -> None:
        server = _Server(echo=echo)
        with make_client(server) as c, pytest.raises(UnsupportedFeature) as exc:
            _WRITE_CALLS[name](c, response_format=_FORMAT)
        assert exc.value.task_id == 10
        assert server.paths[-1].startswith("POST ")

    @pytest.mark.parametrize("name", list(_WRITE_CALLS))
    def test_missing_echo_of_a_text_request_raises_with_task_id(
        self, make_client: Callable[..., AgentClient], name: str
    ) -> None:
        server = _Server(echo={})
        with make_client(server) as c, pytest.raises(UnsupportedFeature) as exc:
            _WRITE_CALLS[name](c, response_format={"type": "text"})
        assert exc.value.task_id == 10
        assert server.paths[-1].startswith("POST ")

    def test_text_echo_matches_a_clear(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/v1/features":
                return httpx.Response(200, json=response("features"))
            return httpx.Response(202, json=response("append_task_response_format"))

        with make_client(handler) as c:
            result = c.tasks.append(
                10, agent_id=7, message="hi", response_format={"type": "text"}
            )
        assert result.run_id == "run_4e5f6a7b"

    def test_create_fixture_echo_matches(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/v1/features":
                return httpx.Response(200, json=response("features"))
            return httpx.Response(202, json=response("create_task_response_format"))

        with make_client(handler) as c:
            result = c.tasks.create(agent_id=7, message="hi", response_format=_FORMAT)
        assert result.task_id == 10
        assert result.run_id == "run_0a1b2c3d"


def test_unsupported_feature_accepts_http_status_like_its_siblings() -> None:
    exc = UnsupportedFeature("unsupported_feature", "m", 500, task_id=3)
    assert exc.http_status == 500
    assert exc.task_id == 3


def test_unsupported_schema_accepts_http_status_like_its_siblings() -> None:
    exc = UnsupportedSchema("unsupported_schema", "m", 500, rule="S3", path="/a")
    assert exc.http_status == 500
    assert (exc.rule, exc.path) == ("S3", "/a")
