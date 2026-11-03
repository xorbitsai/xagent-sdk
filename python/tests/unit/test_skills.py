"""Tests for UserClient.skills (SkillsAPI).

Each block below pins one invariant of the skill methods: how a name
becomes a path, which names are refused before any request, how success
and failure bodies are parsed, and which error class each failure maps to.
Wire bodies come from ``shared/fixtures/v1/`` so the tests consume the
same JSON other language clients will use.
"""

import copy
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from xagent_sdk import (
    InvalidAPIKey,
    InvalidInput,
    MalformedResponse,
    SkillAlreadyExists,
    SkillDetail,
    SkillNotFound,
    SkillSummary,
    UserClient,
    XAgentError,
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
}
_OK_FIXTURES: dict[tuple[str, str], str] = {
    ("skills", "list"): "skills_list",
    ("skills", "get"): "skill_detail",
    ("skills", "create"): "skill_write",
    ("skills", "update"): "skill_write",
    ("skills", "delete"): "skill_delete",
}

# HTTP method and raw request path of every new method, for the canonical
# calls above.
_ROUTES: dict[tuple[str, str], tuple[str, bytes]] = {
    ("skills", "list"): ("GET", b"/v1/skills"),
    ("skills", "get"): ("GET", b"/v1/skills/my-skill"),
    ("skills", "create"): ("POST", b"/v1/skills"),
    ("skills", "update"): ("PUT", b"/v1/skills/my-skill"),
    ("skills", "delete"): ("DELETE", b"/v1/skills/my-skill"),
}
_EXPECTED_BODIES: dict[tuple[str, str], dict[str, str] | None] = {
    ("skills", "list"): None,
    ("skills", "get"): None,
    ("skills", "create"): {"name": "my-skill", "skill_md": _SKILL_MD},
    ("skills", "update"): {"skill_md": _SKILL_MD},
    ("skills", "delete"): None,
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
            "team",
            "external",
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

    def test_skill_source_accepts_unknown_value(self) -> None:
        # ``source`` is a plain string: a value the server starts sending
        # later must pass through instead of failing the whole list.
        body = copy.deepcopy(response("skills_list"))
        body[0]["source"] = "marketplace"

        with _make_user(lambda req: httpx.Response(200, json=body)) as c:
            parsed = c.skills.list()

        assert parsed == [SkillSummary(**item) for item in body]
        assert parsed[0].source == "marketplace"

    @pytest.mark.parametrize("method", ["create", "update"])
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


# (method, status, fixture, expected class): every failure the skill
# methods are documented to raise, through the shared error fixtures.
_ERROR_CASES: list[tuple[str, int, str, type[XAgentError]]] = [
    ("get", 404, "skill_not_found", SkillNotFound),
    ("update", 404, "skill_not_found", SkillNotFound),
    ("delete", 404, "skill_not_found", SkillNotFound),
    ("create", 409, "skill_already_exists", SkillAlreadyExists),
    ("create", 422, "validation_422", InvalidInput),
    ("update", 400, "validation_422", InvalidInput),
    ("list", 401, "invalid_api_key", InvalidAPIKey),
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
            _CALLS[("skills", method)](c)
        assert isinstance(exc.value, expected)
        assert exc.value.http_status == status


_AGENT_METHODS_BEFORE_SKILLS = frozenset(
    {"list", "create", "create_from_template", "rotate_key"}
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
            _AGENT_METHODS_BEFORE_SKILLS | {m for (ns, m) in _ROUTES if ns == "agents"}
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
        if expected_body is None:
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
