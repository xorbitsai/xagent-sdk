"""Tests for UserClient.skills (SkillsAPI) and the agent skill methods.

The agent skill methods are ``UserClient.agents.get_skills()`` and
``UserClient.agents.replace_skills()``.

Each block below pins one invariant of the skill methods: how a name
becomes a path, which names are refused before any request, how success
and failure bodies are parsed, and which error class each failure maps to.
Wire bodies come from ``shared/fixtures/v1/`` so the tests consume the
same JSON other language clients will use.
"""

import copy
import json
from collections.abc import Callable
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from typing import Any

import httpx
import pytest

from xagent_sdk import (
    AgentNotFound,
    InternalError,
    InvalidAPIKey,
    InvalidInput,
    MalformedResponse,
    SkillAlreadyExists,
    SkillDetail,
    SkillNotFound,
    SkillSummary,
    UserClient,
    XAgentError,
    XAgentTransportError,
)
from xagent_sdk._agents import AgentsAPI
from xagent_sdk._skills import SkillsAPI

from ._fixtures import error_envelope, response

_Handler = Callable[[httpx.Request], httpx.Response]
_Call = Callable[[UserClient], object]

_SKILL_MD = "---\nname: my-skill\ndescription: d\n---\n\n# My skill\n"


def _make_user(handler: _Handler) -> UserClient:
    return UserClient(
        personal_key="xag_personal_p_s",
        base_url="https://test.example",
        transport=httpx.MockTransport(handler),
    )


# One canonical call per new method, and the fixture a real server answers
# it with. Shared by the route, parsing and error-mapping tests below.
_CALLS: dict[tuple[str, str], _Call] = {
    ("skills", "list"): lambda u: u.skills.list(),
    ("skills", "get"): lambda u: u.skills.get("my-skill"),
    ("skills", "create"): lambda u: u.skills.create(
        name="my-skill", skill_md=_SKILL_MD
    ),
    ("skills", "update"): lambda u: u.skills.update("my-skill", skill_md=_SKILL_MD),
    ("skills", "delete"): lambda u: u.skills.delete("my-skill"),
    ("skills", "upload"): lambda u: u.skills.upload(
        filename="my-skill.md", content=_SKILL_MD.encode(), name="my-skill"
    ),
    ("agents", "get_skills"): lambda u: u.agents.get_skills(7),
    ("agents", "replace_skills"): lambda u: u.agents.replace_skills(
        7, ["weekly-report"]
    ),
}
_OK_FIXTURES: dict[tuple[str, str], str] = {
    ("skills", "list"): "skills_list",
    ("skills", "get"): "skill_detail",
    ("skills", "create"): "skill_write",
    ("skills", "update"): "skill_write",
    ("skills", "delete"): "skill_delete",
    ("skills", "upload"): "skill_write",
    ("agents", "get_skills"): "agent_skills",
    ("agents", "replace_skills"): "agent_skills_update",
}

# HTTP method and raw request path of every new method, for the canonical
# calls above.
_ROUTES: dict[tuple[str, str], tuple[str, bytes]] = {
    ("skills", "list"): ("GET", b"/v1/skills"),
    ("skills", "get"): ("GET", b"/v1/skills/my-skill"),
    ("skills", "create"): ("POST", b"/v1/skills"),
    ("skills", "update"): ("PUT", b"/v1/skills/my-skill"),
    ("skills", "delete"): ("DELETE", b"/v1/skills/my-skill"),
    ("skills", "upload"): ("POST", b"/v1/skills/upload"),
    ("agents", "get_skills"): ("GET", b"/v1/agents/7/skills"),
    ("agents", "replace_skills"): ("PUT", b"/v1/agents/7/skills"),
}
# ``upload`` sends multipart, not JSON, so its row holds this marker instead of
# an expected JSON body. ``test_upload_multipart_shape`` pins its fields, so the
# route test only checks the content type.
_MULTIPART_BODY: Any = object()
_EXPECTED_BODIES: dict[tuple[str, str], dict[str, Any] | None] = {
    ("skills", "list"): None,
    ("skills", "get"): None,
    ("skills", "create"): {"name": "my-skill", "skill_md": _SKILL_MD},
    ("skills", "update"): {"skill_md": _SKILL_MD},
    ("skills", "delete"): None,
    ("skills", "upload"): _MULTIPART_BODY,
    ("agents", "get_skills"): None,
    ("agents", "replace_skills"): {"skills": ["weekly-report"]},
}

# Methods that take a skill name, called with an arbitrary name.
_NAME_CALLS: dict[str, Callable[[UserClient, str], object]] = {
    "get": lambda u, n: u.skills.get(n),
    "update": lambda u, n: u.skills.update(n, skill_md=_SKILL_MD),
    "delete": lambda u, n: u.skills.delete(n),
}
_NAME_FIXTURES = {
    "get": "skill_detail",
    "update": "skill_write",
    "delete": "skill_delete",
}


# (method, name, raw path sent): ``None`` means the name is refused before
# any request. The two control cells are names that look similar but stay
# inside one path segment, so they are sent.
_NAME_CASES: list[tuple[str, str, bytes | None]] = [
    *[
        (method, name, None)
        for method in ("get", "update", "delete")
        for name in ("", ".", "..", "a/b")
    ],
    ("get", "...", b"/v1/skills/..."),
    ("get", "a.", b"/v1/skills/a."),
]


class TestReads:
    def test_skill_read_happy_paths(self) -> None:
        # list() and get() map every wire field, in server order.
        listing = response("skills_list")
        detail = response("skill_detail")

        def handler(req: httpx.Request) -> httpx.Response:
            body = detail if req.url.path.endswith("/my-skill") else listing
            return httpx.Response(200, json=body)

        with _make_user(handler) as c:
            summaries = c.skills.list()
            got = c.skills.get("my-skill")

        assert summaries == [SkillSummary(**item) for item in listing]
        assert [s.source for s in summaries] == [
            "personal",
            "builtin",
            "external",
            "team",
        ]
        assert got == SkillDetail(**detail)
        assert got.files == ["SKILL.md", "templates/report.md"]


class TestNameHandling:
    @pytest.mark.parametrize("method", ["get", "update", "delete"])
    def test_skill_name_is_path_encoded(self, method: str) -> None:
        # A name with "?", "#" or "%" stays inside one path segment and
        # cannot leak into the query string.
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response(_NAME_FIXTURES[method]))

        with _make_user(handler) as c:
            _NAME_CALLS[method](c, "a?x=1#y%z")

        url = captured[0].url
        assert url.query == b""
        assert url.raw_path.endswith(b"/v1/skills/a%3Fx%3D1%23y%25z")

    @pytest.mark.parametrize(("method", "name", "sent_path"), _NAME_CASES)
    def test_route_changing_skill_names_rejected_locally(
        self, method: str, name: str, sent_path: bytes | None
    ) -> None:
        assert sum(1 for case in _NAME_CASES if case[2] is None) == 12
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response(_NAME_FIXTURES[method]))

        with _make_user(handler) as c:
            if sent_path is None:
                with pytest.raises(ValueError, match="not a skill name"):
                    _NAME_CALLS[method](c, name)
                assert len(captured) == 0
            else:
                _NAME_CALLS[method](c, name)
                assert [r.url.raw_path for r in captured] == [sent_path]


class TestParsing:
    @pytest.mark.parametrize(
        ("reply_kwargs", "message_part"),
        [
            ({"json": {"skills": []}}, "expected a JSON array"),
            (
                {
                    "content": b"null",
                    "headers": {"content-type": "application/json"},
                },
                "expected a JSON array",
            ),
            ({"json": "x"}, "expected a JSON array"),
            ({"json": [{"name": "a"}]}, "did not match the expected shape"),
            # Control: a real empty list is an answer, not a failure.
            ({"json": []}, None),
        ],
        ids=["object", "null", "string", "entry-missing-fields", "empty-array"],
    )
    def test_skill_list_malformed_bodies(
        self, reply_kwargs: dict[str, Any], message_part: str | None
    ) -> None:
        with _make_user(lambda req: httpx.Response(200, **reply_kwargs)) as c:
            if message_part is None:
                assert c.skills.list() == []
            else:
                with pytest.raises(MalformedResponse, match=message_part) as exc:
                    c.skills.list()
                assert exc.value.http_status is None

    @pytest.mark.parametrize(
        "body",
        [{}, [], {"name": "a"}],
        ids=["empty-object", "array", "missing-fields"],
    )
    def test_skill_detail_malformed_bodies(self, body: Any) -> None:
        with (
            _make_user(lambda req: httpx.Response(200, json=body)) as c,
            pytest.raises(MalformedResponse) as exc,
        ):
            c.skills.get("my-skill")
        assert exc.value.http_status is None

    def test_skill_detail_accepts_unknown_source(self) -> None:
        body = copy.deepcopy(response("skill_detail"))
        body["source"] = "marketplace"

        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            got = c.skills.get("my-skill")

        assert got.source == "marketplace"

    def test_skill_source_accepts_unknown_value(self) -> None:
        # ``source`` is a plain string: a value the server starts sending
        # later must pass through instead of failing the whole list.
        body = copy.deepcopy(response("skills_list"))
        body[0]["source"] = "marketplace"

        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            parsed = c.skills.list()

        assert parsed == [SkillSummary(**item) for item in body]
        assert parsed[0].source == "marketplace"

    @pytest.mark.parametrize("method", ["create", "update", "upload"])
    @pytest.mark.parametrize(
        ("body", "valid"),
        [(response("skill_write"), True), ({}, False), ([], False)],
        ids=["fixture", "empty-object", "empty-array"],
    )
    def test_skill_write_response_parsing(
        self, method: str, body: object, valid: bool
    ) -> None:
        call = _CALLS[("skills", method)]
        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            if valid:
                result = call(c)
                assert isinstance(result, SkillSummary)
                fixture = response("skill_write")
                assert result.name == fixture["name"]
                assert result.description == fixture["description"]
                assert result.when_to_use == fixture["when_to_use"]
                assert result.tags == fixture["tags"]
                assert result.source == fixture["source"]
            else:
                with pytest.raises(MalformedResponse) as exc:
                    call(c)
                assert exc.value.http_status is None

    @pytest.mark.parametrize(
        ("body", "confirmed"),
        [({"name": "x"}, True), ({}, False), ("ok", False), ([], False)],
        ids=["confirmed", "empty-object", "string", "empty-array"],
    )
    def test_skill_delete_requires_confirmation_body(
        self, body: object, confirmed: bool
    ) -> None:
        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            if confirmed:
                assert c.skills.delete("x") is None
            else:
                with pytest.raises(MalformedResponse):
                    c.skills.delete("x")


# The error table names methods only; every method name is unique across the
# namespaces in ``_CALLS``.
_CALL_BY_METHOD: dict[str, _Call] = {m: call for (_, m), call in _CALLS.items()}
assert len(_CALL_BY_METHOD) == len(_CALLS)

# (method, status, fixture, expected class): the failures the skill
# methods are documented to raise, through the shared error fixtures.
_ERROR_CASES: list[tuple[str, int, str, type[XAgentError]]] = [
    ("get", 404, "skill_not_found", SkillNotFound),
    ("update", 404, "skill_not_found", SkillNotFound),
    ("delete", 404, "skill_not_found", SkillNotFound),
    ("create", 409, "skill_already_exists", SkillAlreadyExists),
    ("create", 422, "validation_422", InvalidInput),
    ("update", 400, "validation_422", InvalidInput),
    ("list", 401, "invalid_api_key", InvalidAPIKey),
    ("upload", 409, "skill_already_exists", SkillAlreadyExists),
    ("upload", 413, "validation_422", InvalidInput),
    ("get_skills", 404, "agent_not_found", AgentNotFound),
    ("replace_skills", 400, "validation_422", InvalidInput),
    ("replace_skills", 422, "validation_422", InvalidInput),
]


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("method", "status", "fixture", "expected"),
        _ERROR_CASES,
        ids=[f"{c[0]}-{c[1]}" for c in _ERROR_CASES],
    )
    def test_new_method_error_mapping(
        self, method: str, status: int, fixture: str, expected: type[XAgentError]
    ) -> None:
        # The table grows by 2 rows when ``upload`` exists and by 3 when
        # the agent skill methods exist; deleting a row breaks this sum.
        assert len(_ERROR_CASES) == 7 + 2 * hasattr(SkillsAPI, "upload") + 3 * hasattr(
            AgentsAPI, "get_skills"
        )
        body = error_envelope(fixture)
        with (
            _make_user(lambda req: httpx.Response(status, json=body)) as c,
            pytest.raises(XAgentError) as exc,
        ):
            _CALL_BY_METHOD[method](c)
        assert isinstance(exc.value, expected)
        assert exc.value.http_status == status


_LEGACY_AGENT_METHODS = frozenset(
    {
        "list",
        "create",
        "create_from_template",
        "rotate_key",
        "list_keys",
        "revoke_key",
    }
)


class TestRoutes:
    @pytest.mark.parametrize(("namespace", "method"), list(_ROUTES))
    def test_new_methods_send_expected_method_and_path(
        self, namespace: str, method: str
    ) -> None:
        # Member binding: a public method added to either class has to be
        # registered in ``_ROUTES`` (and so is covered by the tests here
        # and in ``TestUnexpectedSuccessResponses``) or deliberately added
        # to the set of older agent methods.
        assert {m for (ns, m) in _ROUTES if ns == "skills"} == {
            n for n in vars(SkillsAPI) if not n.startswith("_")
        }
        assert {n for n in vars(AgentsAPI) if not n.startswith("_")} == (
            _LEGACY_AGENT_METHODS | {m for (ns, m) in _ROUTES if ns == "agents"}
        )

        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response(_OK_FIXTURES[(namespace, method)]))

        with _make_user(handler) as c:
            _CALLS[(namespace, method)](c)

        http_method, raw_path = _ROUTES[(namespace, method)]
        assert len(captured) == 1
        assert captured[0].method == http_method
        assert captured[0].url.raw_path == raw_path
        expected_body = _EXPECTED_BODIES[(namespace, method)]
        if expected_body is _MULTIPART_BODY:
            content_type = captured[0].headers["content-type"]
            assert content_type.startswith("multipart/form-data; boundary=")
        elif expected_body is None:
            assert captured[0].content == b""
        else:
            assert json.loads(captured[0].content) == expected_body


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


class TestUnexpectedSuccessResponses:
    @pytest.mark.parametrize(("namespace", "method"), list(_ROUTES))
    @pytest.mark.parametrize("kind", list(_UNEXPECTED_SUCCESS))
    def test_new_methods_reject_unexpected_success_responses(
        self, namespace: str, method: str, kind: str
    ) -> None:
        assert len(_ROUTES) * len(_UNEXPECTED_SUCCESS) == 4 * len(_ROUTES)
        status, kwargs, expected_status = _UNEXPECTED_SUCCESS[kind]
        with (
            _make_user(lambda req: httpx.Response(status, **kwargs)) as c,
            pytest.raises(MalformedResponse) as exc,
        ):
            _CALLS[(namespace, method)](c)
        assert exc.value.http_status == expected_status


def _multipart_parts(req: httpx.Request) -> dict[str, tuple[str | None, bytes]]:
    """Decode a multipart request into ``{field: (filename, value)}``."""
    raw = (
        b"Content-Type: "
        + req.headers["content-type"].encode()
        + b"\r\n\r\n"
        + req.content
    )
    message = BytesParser(policy=default).parsebytes(raw)
    return {
        part.get_param("name", header="content-disposition"): (
            part.get_filename(),
            part.get_payload(decode=True),
        )
        for part in message.iter_parts()
    }


_ZIP_BYTES = b"PK\x03\x04\xff\x00abc\r\n--x"


def _raise_connect_error(req: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("dns dead")


class TestUpload:
    @pytest.mark.parametrize(
        ("name", "expected_name"),
        [(None, None), ("", (None, b"")), ("x", (None, b"x"))],
        ids=["none", "empty", "given"],
    )
    def test_upload_multipart_shape(
        self, name: str | None, expected_name: tuple[None, bytes] | None
    ) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("skill_write"))

        with _make_user(handler) as c:
            c.skills.upload(filename="my-skill.zip", content=_ZIP_BYTES, name=name)

        assert len(captured) == 1
        content_type = captured[0].headers["content-type"]
        assert content_type.startswith("multipart/form-data; boundary=")
        parts = _multipart_parts(captured[0])
        assert parts["file"] == ("my-skill.zip", _ZIP_BYTES)
        if expected_name is None:
            assert "name" not in parts
        else:
            assert parts["name"] == expected_name

    @pytest.mark.parametrize(
        ("handler", "expected", "expected_status"),
        [
            (_raise_connect_error, XAgentTransportError, None),
            (
                lambda req: httpx.Response(
                    413,
                    json={
                        "error": {
                            "code": "invalid_input",
                            "message": "Upload exceeds 50 MiB limit.",
                        }
                    },
                ),
                InvalidInput,
                413,
            ),
            (
                lambda req: httpx.Response(400, json={"detail": "x"}),
                InternalError,
                400,
            ),
        ],
        ids=["connect-error", "413-envelope", "400-no-envelope"],
    )
    def test_upload_failure_paths(
        self,
        handler: _Handler,
        expected: type[XAgentError],
        expected_status: int | None,
    ) -> None:
        with (
            _make_user(handler) as c,
            pytest.raises(XAgentError) as exc,
        ):
            c.skills.upload(filename="my-skill.zip", content=_ZIP_BYTES)
        assert type(exc.value) is expected
        assert exc.value.http_status == expected_status

    def test_upload_sends_the_file_in_small_chunks(self) -> None:
        # ``httpx.MockTransport`` reads the whole body before calling its
        # handler, so the chunks are observed from a bare transport that
        # iterates the request stream itself.
        content = b"skill-body-bytes" * (200 * 1024 // 16)
        chunk_sizes: list[int] = []
        body = bytearray()

        class _Recorder(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                for chunk in request.stream:  # type: ignore[union-attr]
                    chunk_sizes.append(len(chunk))
                    body.extend(chunk)
                return httpx.Response(200, json=response("skill_write"))

        with UserClient(
            personal_key="xag_personal_p_s",
            base_url="https://test.example",
            transport=_Recorder(),
        ) as c:
            c.skills.upload(filename="big.zip", content=content)

        assert len(content) == 200 * 1024
        assert max(chunk_sizes) <= 65536
        assert content in bytes(body)

    @pytest.mark.parametrize(
        ("filename", "sent"),
        [
            (Path("/home/u/clients/acme/x.zip"), "x.zip"),
            ("/home/u/clients/acme/x.zip", "x.zip"),
            ("clients\\acme\\x.md", "x.md"),
            ("C:\\Users\\u\\x.zip", "x.zip"),
        ],
        ids=["path-object", "posix-full-path", "windows-relative", "windows-absolute"],
    )
    def test_upload_sends_only_the_last_path_part(
        self, filename: str | Path, sent: str
    ) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=response("skill_write"))

        with _make_user(handler) as c:
            c.skills.upload(filename=filename, content=_ZIP_BYTES)

        assert len(captured) == 1
        assert _multipart_parts(captured[0])["file"] == (sent, _ZIP_BYTES)

    @pytest.mark.parametrize(
        "filename", ["", "dir/"], ids=["empty", "trailing-separator"]
    )
    def test_upload_filename_without_last_part_rejected_locally(
        self, filename: str
    ) -> None:
        calls: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req)
            return httpx.Response(200, json=response("skill_write"))

        with (
            _make_user(handler) as c,
            pytest.raises(ValueError, match="filename must be a non-empty string"),
        ):
            c.skills.upload(filename=filename, content=_ZIP_BYTES)
        assert calls == []
