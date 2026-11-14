"""Every JSON endpoint reports a redirect, an unexpected success status or a
non-JSON success body as ``MalformedResponse``.

The client does not follow redirects, so a base URL that answers 301 (for
example a browser entry host in front of the API) reaches the success path.
Every method decodes its success body through ``_json_body``, so none of
these cases escapes as ``json.JSONDecodeError``, which is not an
``XAgentError``. The ``skills.*``, ``agents.get_skills()`` and
``agents.replace_skills()`` methods are covered in ``test_skills.py``, and
``agents.get_connectors()`` and ``agents.replace_connectors()`` in
``test_agent_connectors.py``.
"""

import pathlib
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from xagent_sdk import AgentClient, MalformedResponse, UserClient, XAgentError
from xagent_sdk.cloud import WorkspaceClient

from .test_agent_connectors import _ROUTES as AGENT_CONNECTOR_ROUTES
from .test_mcp_servers import _ROUTES as MCP_SERVER_ROUTES
from .test_skills import _ROUTES as SKILL_ROUTES

_BASE = "https://test.example"

Call = Callable[[Any], object]

# (id, client kind, call, the one success status the server sends)
_CASES: list[tuple[str, str, Call, int]] = [
    ("me", "user", lambda c: c.me(), 200),
    ("agents.list", "user", lambda c: c.agents.list(), 200),
    (
        "agents.create",
        "user",
        lambda c: c.agents.create(name="a", instructions="b"),
        200,
    ),
    (
        "agents.create_from_template",
        "user",
        lambda c: c.agents.create_from_template("t"),
        200,
    ),
    ("agents.rotate_key", "user", lambda c: c.agents.rotate_key(1), 200),
    ("agents.list_keys", "user", lambda c: c.agents.list_keys(1), 200),
    ("agents.revoke_key", "user", lambda c: c.agents.revoke_key(1, 2), 200),
    ("templates.list", "user", lambda c: c.templates.list(), 200),
    ("templates.get", "user", lambda c: c.templates.get("t"), 200),
    (
        "user tasks.create",
        "user",
        lambda c: c.tasks.create(agent_id=1, message="hi"),
        202,
    ),
    (
        "agent tasks.create",
        "agent",
        lambda c: c.tasks.create(agent_id=1, message="hi"),
        202,
    ),
    (
        "tasks.append",
        "agent",
        lambda c: c.tasks.append(1, agent_id=1, message="hi"),
        202,
    ),
    (
        "tasks.reply",
        "agent",
        lambda c: c.tasks.reply(1, agent_id=1, message="hi"),
        202,
    ),
    ("tasks.cancel", "agent", lambda c: c.tasks.cancel(1), 200),
    ("tasks.get", "agent", lambda c: c.tasks.get(1), 200),
    (
        "tasks features",
        "agent",
        lambda c: c.tasks.create(
            agent_id=1, message="hi", response_format={"type": "text"}
        ),
        200,
    ),
    ("tasks.steps", "agent", lambda c: c.tasks.steps(1), 200),
    ("workspace agents.list", "workspace", lambda c: c.agents.list(), 200),
    (
        "workspace agents.create",
        "workspace",
        lambda c: c.agents.create(name="a", instructions="b"),
        200,
    ),
    (
        "workspace agents.create_from_template",
        "workspace",
        lambda c: c.agents.create_from_template("t"),
        200,
    ),
    (
        "workspace agents.rotate_key",
        "workspace",
        lambda c: c.agents.rotate_key(1),
        200,
    ),
    (
        "workspace agents.list_keys",
        "workspace",
        lambda c: c.agents.list_keys(1),
        200,
    ),
    (
        "workspace agents.revoke_key",
        "workspace",
        lambda c: c.agents.revoke_key(1, 2),
        200,
    ),
    ("workspace templates.list", "workspace", lambda c: c.templates.list(), 200),
    ("workspace templates.get", "workspace", lambda c: c.templates.get("t"), 200),
]

_IDS = [case[0] for case in _CASES]


def _client(kind: str, handler: Callable[[httpx.Request], httpx.Response]) -> Any:
    transport = httpx.MockTransport(handler)
    if kind == "user":
        return UserClient(
            personal_key="xag_personal_p_s", base_url=_BASE, transport=transport
        )
    if kind == "agent":
        return AgentClient(api_key="xag_p_s", base_url=_BASE, transport=transport)
    return WorkspaceClient(
        workspace_key="xag_workspace_p_s", base_url=_BASE, transport=transport
    )


def _run(kind: str, call: Call, resp: httpx.Response) -> MalformedResponse:
    with (
        _client(kind, lambda req: resp) as c,  # noqa: ARG005
        pytest.raises(MalformedResponse) as exc_info,
    ):
        call(c)
    assert isinstance(exc_info.value, XAgentError)
    return exc_info.value


@pytest.mark.parametrize(("name", "kind", "call", "ok"), _CASES, ids=_IDS)
def test_redirect_raises_malformed_response_with_status(
    name: str, kind: str, call: Call, ok: int
) -> None:
    exc = _run(
        kind,
        call,
        httpx.Response(
            301,
            headers={"location": "https://elsewhere.example/v1/me"},
            text="<html><body>Moved</body></html>",
        ),
    )
    assert exc.http_status == 301
    assert f"expected HTTP {ok}" in exc.message


@pytest.mark.parametrize(("name", "kind", "call", "ok"), _CASES, ids=_IDS)
def test_non_json_success_body_raises_malformed_response(
    name: str, kind: str, call: Call, ok: int
) -> None:
    exc = _run(kind, call, httpx.Response(ok, text="<html>proxy page</html>"))
    assert exc.http_status is None
    assert "is not JSON" in exc.message


@pytest.mark.parametrize(("name", "kind", "call", "ok"), _CASES, ids=_IDS)
def test_other_success_status_raises_malformed_response(
    name: str, kind: str, call: Call, ok: int
) -> None:
    other = 202 if ok == 200 else 200
    if name == "tasks.cancel":
        # Both 200 (applied) and 202 (accepted) are its success statuses.
        other = 204
    exc = _run(kind, call, httpx.Response(other, json={}))
    assert exc.http_status == other


_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "xagent_sdk"
# The only two lines that may decode a body themselves: the helper, and the
# error mapping, which falls back to the raw text on a non-JSON error body.
_ALLOWED_DECODES = [
    ("_base.py", "return resp.json()"),
    ("errors.py", "body: Any = resp.json()"),
]


def test_no_method_decodes_a_success_body_directly() -> None:
    found = [
        (str(path.relative_to(_SRC)), line.strip())
        for path in sorted(_SRC.rglob("*.py"))
        for line in path.read_text().splitlines()
        if re.search(r"\.json\(\)", line) and not line.lstrip().startswith("#")
    ]
    assert found == _ALLOWED_DECODES


def test_case_list_covers_every_helper_call() -> None:
    # Binds _CASES and the skill and connector tests' route lists to the code:
    # a new JSON method must be registered in one of them, or this count goes
    # stale.
    calls = sum(
        len(re.findall(r"_json_body\(resp,", path.read_text()))
        for path in _SRC.rglob("*.py")
    )
    # tasks.create is one call site shared by the user and agent clients.
    assert calls == (
        len(_CASES)
        - 1
        + len(SKILL_ROUTES)
        + len(MCP_SERVER_ROUTES)
        + len(AGENT_CONNECTOR_ROUTES)
    )
