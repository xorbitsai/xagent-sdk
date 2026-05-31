"""Invariant: AgentClient and UserClient instances do not share state.

Each public client owns its own httpx.Client (default headers, connection
pool). Constructing both in the same process must produce two distinct
``Authorization`` headers; one client's revoke / rotate must not affect
the other. This module pins the property mechanically so a future
refactor cannot silently share an httpx instance across clients.
"""

import httpx

from xagent_sdk import AgentClient, UserClient


def _ok(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


def test_two_clients_default_headers_isolated() -> None:
    u = UserClient(
        personal_key="xag_personal_p_s",
        base_url="https://x",
        transport=httpx.MockTransport(_ok),
    )
    a = AgentClient(
        api_key="xag_p_s",
        base_url="https://x",
        transport=httpx.MockTransport(_ok),
    )
    try:
        u_auth = u._http._client.headers["Authorization"]
        a_auth = a._http._client.headers["Authorization"]
        assert u_auth == "Bearer xag_personal_p_s"
        assert a_auth == "Bearer xag_p_s"
        assert u_auth != a_auth
        # Two distinct httpx.Client instances (not aliasing)
        assert u._http._client is not a._http._client
    finally:
        u.close()
        a.close()


def test_close_one_does_not_close_other() -> None:
    u = UserClient(
        personal_key="xag_personal_p_s",
        base_url="https://x",
        transport=httpx.MockTransport(_ok),
    )
    a = AgentClient(
        api_key="xag_p_s",
        base_url="https://x",
        transport=httpx.MockTransport(_ok),
    )
    u.close()
    assert u._http._client.is_closed is True
    assert a._http._client.is_closed is False
    a.close()
