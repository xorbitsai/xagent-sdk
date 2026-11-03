"""Tests for UserClient construction, env-var fallback, the me() probe, and the
tasks namespace.
"""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from xagent_sdk import AgentClient, InvalidAPIKey, UserClient, UserPrincipal
from xagent_sdk.tasks import TasksAPI

from ._fixtures import error_envelope, response


def _me_handler(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=response("me_user"))


def _401_handler(req: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json=error_envelope("invalid_api_key"))


class TestConstruction:
    def test_explicit(self) -> None:
        with UserClient(
            personal_key="xag_personal_p_s",
            base_url="https://test.example",
            transport=httpx.MockTransport(_me_handler),
        ) as c:
            assert c._http._client.headers["Authorization"] == "Bearer xag_personal_p_s"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XAGENT_PERSONAL_KEY", "xag_personal_envkey_envsec")
        monkeypatch.setenv("XAGENT_BASE_URL", "https://envhost")
        c = UserClient(transport=httpx.MockTransport(_me_handler))
        assert (
            c._http._client.headers["Authorization"]
            == "Bearer xag_personal_envkey_envsec"
        )
        c.close()

    def test_explicit_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XAGENT_PERSONAL_KEY", "envkey")
        monkeypatch.setenv("XAGENT_BASE_URL", "https://envhost")
        c = UserClient(
            personal_key="explicit",
            base_url="https://explicit",
            transport=httpx.MockTransport(_me_handler),
        )
        assert c._http._client.headers["Authorization"] == "Bearer explicit"
        c.close()

    def test_missing_personal_key(self) -> None:
        # The error message should name personal_key specifically so the
        # caller knows which env var / kwarg to set.
        with pytest.raises(ValueError, match="personal_key"):
            UserClient(base_url="https://x")

    def test_missing_base_url(self) -> None:
        with pytest.raises(ValueError, match="base_url"):
            UserClient(personal_key="xag_personal_p_s")

    def test_does_not_read_xagent_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # XAGENT_API_KEY belongs to AgentClient; UserClient must not pick
        # it up by accident. Only XAGENT_PERSONAL_KEY counts.
        monkeypatch.setenv("XAGENT_API_KEY", "runtime-key-not-personal")
        monkeypatch.setenv("XAGENT_BASE_URL", "https://envhost")
        with pytest.raises(ValueError, match="personal_key"):
            UserClient()

    def test_empty_personal_key_does_not_fall_back_to_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An explicit empty key must raise, never resolve to the env value.
        monkeypatch.setenv("XAGENT_PERSONAL_KEY", "xag_personal_envkey_envsec")
        monkeypatch.setenv("XAGENT_BASE_URL", "https://envhost")
        with pytest.raises(ValueError, match="personal_key"):
            UserClient(personal_key="")


class TestMe:
    def test_returns_user_principal(self) -> None:
        with UserClient(
            personal_key="xag_personal_p_s",
            base_url="https://test.example",
            transport=httpx.MockTransport(_me_handler),
        ) as c:
            me = c.me()
        assert isinstance(me, UserPrincipal)
        assert me.principal_type == "user"
        assert me.user_id == 123
        assert me.username == "alex"
        assert me.email == "user@example.com"
        assert me.key_prefix == "abc123"

    def test_me_email_may_be_null(self) -> None:
        # The backend returns email=null for accounts with no email set;
        # UserPrincipal.email is optional, so this must parse cleanly.
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "principal_type": "user",
                    "user_id": 1,
                    "username": "administrator",
                    "email": None,
                    "key_prefix": "abc123",
                },
            )

        with UserClient(
            personal_key="xag_personal_p_s",
            base_url="https://test.example",
            transport=httpx.MockTransport(handler),
        ) as c:
            me = c.me()
        assert me.username == "administrator"
        assert me.email is None

    def test_401_raises_invalid_api_key(self) -> None:
        with (
            UserClient(
                personal_key="xag_personal_p_s",
                base_url="https://test.example",
                transport=httpx.MockTransport(_401_handler),
            ) as c,
            pytest.raises(InvalidAPIKey),
        ):
            c.me()


class TestLifecycle:
    def test_context_manager_closes(self) -> None:
        c = UserClient(
            personal_key="xag_personal_p_s",
            base_url="https://test.example",
            transport=httpx.MockTransport(_me_handler),
        )
        assert c._http._client.is_closed is False
        with c:
            pass
        assert c._http._client.is_closed is True


# One row per plain request/response ``TasksAPI`` method; ``events`` (SSE)
# and ``wait`` / ``run`` (polling loops) are exercised by their own tests.
_TASK_CALLS: dict[str, tuple[Callable[[Any], object], int, str]] = {
    "create": (lambda c: c.tasks.create(agent_id=7, message="hi"), 202, "create_task"),
    "append": (
        lambda c: c.tasks.append(10, agent_id=7, message="next"),
        202,
        "append_task",
    ),
    "reply": (
        lambda c: c.tasks.reply(10, agent_id=7, message="answer"),
        202,
        "reply_task",
    ),
    "get": (lambda c: c.tasks.get(10), 200, "task_info_completed"),
    "steps": (lambda c: c.tasks.steps(10), 200, "steps_full"),
}


def test_task_call_table_covers_every_plain_tasks_method() -> None:
    public = {
        n
        for n in vars(TasksAPI)
        if not n.startswith("_") and callable(getattr(TasksAPI, n))
    }
    assert public - {"events", "wait", "run"} == set(_TASK_CALLS)
    assert len(_TASK_CALLS) == 5


@pytest.mark.parametrize("method", list(_TASK_CALLS))
def test_user_client_tasks_send_personal_key(method: str) -> None:
    call, status, fixture = _TASK_CALLS[method]
    seen: dict[str, httpx.Request] = {}

    def make_handler(label: str) -> Callable[[httpx.Request], httpx.Response]:
        def handler(req: httpx.Request) -> httpx.Response:
            seen[label] = req
            return httpx.Response(status, json=response(fixture))

        return handler

    with UserClient(
        personal_key="xag_personal_p_s",
        base_url="https://test.example",
        transport=httpx.MockTransport(make_handler("user")),
    ) as user:
        call(user)
    with AgentClient(
        api_key="xag_a_s",
        base_url="https://test.example",
        transport=httpx.MockTransport(make_handler("agent")),
    ) as agent:
        call(agent)

    assert seen["user"].method == seen["agent"].method
    assert seen["user"].url.path == seen["agent"].url.path
    assert seen["user"].headers["Authorization"] == "Bearer xag_personal_p_s"
    assert seen["agent"].headers["Authorization"] == "Bearer xag_a_s"
    assert seen["user"].content == seen["agent"].content
