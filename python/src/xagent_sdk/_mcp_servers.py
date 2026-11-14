"""The ``client.mcp_servers`` namespace exposed on ``UserClient``.

An MCP connector (an "MCP server" in the API) gives agents tools from an
external MCP endpoint. This namespace lets the personal-key holder read
the connectors they can see, and create, change, switch on or off and
delete their own.

This module is internal -- consumers reach it via ``user_client.mcp_servers``
-- so the class is exported via the user client and not re-exported from
``xagent_sdk.__init__``.
"""

import math
import operator
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from xagent_sdk._base import _json_body
from xagent_sdk.types import (
    McpServer,
    McpServerTools,
    _parse_mcp_server_delete,
    _parse_mcp_server_list,
    _parse_mcp_server_read,
    _parse_mcp_server_tools,
    _parse_mcp_server_write,
)

if TYPE_CHECKING:
    from xagent_sdk.user_client import UserClient

# ``McpServersAPI.list()`` shadows the ``list`` builtin inside the class body,
# so a ``list[...]`` annotation on a later method cannot resolve to the
# builtin type. Reference it through a module-scope alias instead.
_McpServerList = list[McpServer]


class _Unset(Enum):
    """The type of ``_UNSET``, the default of ``update()`` parameters that
    mean "leave this field as it is"; ``None`` is a real value there."""

    UNSET = "UNSET"


_UNSET = _Unset.UNSET


def _server_path(server_id: int) -> str:
    """The URL path of one connector.

    ``server_id`` must be an integer: the server decodes ``%2F`` back into
    ``/`` before routing, so a string such as ``"5/tools"`` would reach
    another endpoint even after encoding. Anything that is not an integer
    raises ``TypeError`` before a request is sent.
    """
    return f"/v1/mcp-servers/{quote(str(operator.index(server_id)), safe='')}"


class McpServersAPI:
    """The ``user_client.mcp_servers`` namespace.

    ``list()`` and ``get()`` read the connectors the key holder can see:
    their own connectors, switched on or off, and connectors their team
    shares with them; ``list_tools()`` lists the tools of one of them. A
    team connector is read-only for the holder; its ``access`` is ``"team"``.
    ``create()``, ``update()``, ``delete()`` and ``set_enabled()`` change the
    holder's own connectors.

    A connector that does not exist and one the holder cannot see both
    raise ``ConnectorNotFound``. A 404 or 405 without the V1 error envelope
    (the framework's own ``{"detail": ...}``) means the server has no such
    route -- typically a server older than this SDK -- and surfaces as
    ``InternalError`` with that ``http_status``; it is distinguishable from
    ``ConnectorNotFound`` by exception type.
    """

    def __init__(self, client: "UserClient") -> None:
        self._client = client

    def list(self) -> _McpServerList:
        """``GET /v1/mcp-servers`` -- list the connectors the key holder can see.

        Returns the entries in the order the server sends them: the holder's
        own connectors first, including ones switched off, then the ones
        visible only through the team. The SDK does not reorder them. An
        empty list means the server answered that there are none.

        Parsing is strict: if any entry does not match the expected shape,
        the whole call raises ``MalformedResponse`` rather than returning a
        partial list. A body that is not a JSON array also raises it; it is
        never read as an empty list.

        Raises:
            InvalidAPIKey: the key is not accepted by the connector endpoints.
            MalformedResponse: the response status is not 200, the body is
                not JSON, or it is not an array of connectors.
        """
        resp = self._client._request("GET", "/v1/mcp-servers")
        return _parse_mcp_server_list(_json_body(resp, "connector list"))

    def get(self, server_id: int) -> McpServer:
        """``GET /v1/mcp-servers/{server_id}`` -- fetch one connector.

        Raises:
            TypeError: ``server_id`` is not an integer; raised before any
                request is sent.
            ConnectorNotFound: the key holder cannot see a connector with
                this id.
            InvalidAPIKey: the key is not accepted by the connector endpoints.
            MalformedResponse: the response status is not 200, the body is
                not JSON, or it is not a connector object.
        """
        path = _server_path(server_id)
        resp = self._client._request("GET", path)
        return _parse_mcp_server_read(
            _json_body(resp, "connector detail"), "connector detail"
        )

    def list_tools(
        self, server_id: int, *, timeout: float | None = None
    ) -> McpServerTools:
        """``GET /v1/mcp-servers/{server_id}/tools`` -- list a connector's tools.

        The server connects to the connector's MCP endpoint to load the
        tools, so this call is slow compared with the others. It waits a few
        seconds at most for a free slot and then takes up to one
        initialization timeout to start the connector, plus the time to read
        its settings -- over 60 seconds in the worst case with the default
        configuration; a server configured without that timeout sets no
        limit on starting the connector. The slots are shared by all key
        holders of a server process, and each account may have at most two
        listings in progress per server process, across all of its keys and
        connectors.

        ``timeout`` is the read timeout, in seconds, for this one call; it
        must be a finite number greater than 0, otherwise ``ValueError`` is
        raised before any request is sent. When it is not given, this call
        uses the larger of the client's read timeout and 150 seconds, or no
        limit if the client was built without one. The client's own timeout
        is not changed.

        If the client times out first, ``XAgentTransportError`` is raised.
        The server keeps loading the tools, holding its slot and counting
        the listing against your account until it finishes, so do not retry
        immediately.

        Raises:
            ValueError: ``timeout`` is not a finite number greater than 0.
            TypeError: ``server_id`` is not an integer; raised before any
                request is sent.
            ConnectorNotFound: the key holder cannot see a connector with
                this id.
            TransportNotSupported: the connector's transport is not
                ``streamable_http`` or ``sse``, the only ones whose tools the
                API can list.
            ConnectorUnavailable: the connector cannot be used right now;
                ``details["reason"]`` says why (for example ``disabled`` or
                ``team_shared``).
            McpOAuthAuthorizationFailed: the connector's OAuth authorization
                is missing or no longer works; it can only be fixed in the
                web app. ``details["reason"]`` says what is wrong.
            ConnectorToolsUnavailable: the connector was started but did
                not come up (including an unreachable URL) or came up with
                no tools; an empty ``tools`` list is never returned as a
                success. ``details["failures"]`` lists what failed.
            RateLimited: your account already has two listings in progress
                on that server process, including ones whose request ended
                early, or no slot freed up in time; ``retry_after`` is the
                number of seconds the server suggests waiting.
            XAgentTransportError: the client's timeout elapsed or the
                connection failed.
            InvalidAPIKey: the key is not accepted by the connector endpoints.
            MalformedResponse: the response status is not 200, the body is
                not JSON, or it is not a connector tools object.

        A non-empty ``failures`` in the result means part of the loading
        failed although tools were returned. ``McpTool.name`` is the web
        app's display name and is not stable; refer to a tool by its
        ``mcp_name``.
        """
        if timeout is not None and not (math.isfinite(timeout) and timeout > 0):
            raise ValueError("timeout must be a finite number greater than 0")
        path = _server_path(server_id) + "/tools"
        effective = (
            timeout
            if timeout is not None
            else self._client._http.effective_timeout(150.0)
        )
        resp = self._client._request("GET", path, timeout=effective)
        return _parse_mcp_server_tools(_json_body(resp, "connector tools"))

    def create(
        self,
        *,
        name: str,
        transport: str,
        url: str,
        description: str | None = None,
        headers: Mapping[str, str] | None = None,
        auth: Mapping[str, Any] | None = None,
        runtime_input_schema: Mapping[str, Any] | None = None,
        runtime_bindings: Sequence[Mapping[str, Any]] | None = None,
        signed_uploads: Sequence[Mapping[str, Any]] | None = None,
        allow_delegated_authorization: bool = False,
        enabled: bool = True,
    ) -> McpServer:
        """``POST /v1/mcp-servers`` -- create a connector for the key holder.

        ``name``, ``transport``, ``url``, ``allow_delegated_authorization``
        and ``enabled`` are always sent; each of the other parameters is
        sent only when it is not ``None``, so the server's own default
        applies to the ones left out. Mappings and sequences are copied into
        the request body. The SDK does not check the values; the server is
        the authority on them. OAuth authorization can only be completed in
        the web app.

        This call is not idempotent. After a timeout, sending it again
        raises ``ConnectorNameConflict`` if the first attempt went through;
        when ``details["connector_id"]`` is present it is the connector the
        key holder already has under this name. It may be the one the timed
        out attempt created, or one that existed before, so read it with
        ``get()`` and check before relying on it.

        Raises:
            TransportNotSupported: ``transport`` is not ``streamable_http``
                or ``sse``.
            ConnectorNameConflict: the name is already in use or reserved
                (names are unique across all users).
            InvalidInput: the server rejected a value (``http_status`` 400
                or 422).
            ConnectorForbidden: the server refuses the request as not
                permitted for the key holder.
            InvalidAPIKey: the key is not accepted by the connector endpoints.
            MalformedResponse: the response status is not 200, the body is
                not JSON, or it is not a connector object. The connector may
                already exist; do not send the call again.
        """
        body: dict[str, Any] = {
            "name": name,
            "transport": transport,
            "url": url,
            "allow_delegated_authorization": allow_delegated_authorization,
            "enabled": enabled,
        }
        if description is not None:
            body["description"] = description
        if headers is not None:
            body["headers"] = dict(headers)
        if auth is not None:
            body["auth"] = dict(auth)
        if runtime_input_schema is not None:
            body["runtime_input_schema"] = dict(runtime_input_schema)
        if runtime_bindings is not None:
            body["runtime_bindings"] = [dict(x) for x in runtime_bindings]
        if signed_uploads is not None:
            body["signed_uploads"] = [dict(x) for x in signed_uploads]
        resp = self._client._request("POST", "/v1/mcp-servers", json=body)
        return _parse_mcp_server_write(
            _json_body(resp, "connector creation"), "connector creation"
        )

    def update(
        self,
        server_id: int,
        *,
        name: str | _Unset = _UNSET,
        transport: str | _Unset = _UNSET,
        url: str | _Unset = _UNSET,
        description: str | _Unset = _UNSET,
        headers: Mapping[str, str] | _Unset = _UNSET,
        auth: Mapping[str, Any] | _Unset = _UNSET,
        runtime_input_schema: Mapping[str, Any] | None | _Unset = _UNSET,
        runtime_bindings: Sequence[Mapping[str, Any]] | None | _Unset = _UNSET,
        signed_uploads: Sequence[Mapping[str, Any]] | None | _Unset = _UNSET,
        allow_delegated_authorization: bool | _Unset = _UNSET,
    ) -> McpServer:
        """``PATCH /v1/mcp-servers/{server_id}`` -- change one of your connectors.

        Only the fields you pass are sent; a field you leave out keeps its
        stored value. ``name``, ``transport``, ``url``, ``description``,
        ``headers``, ``auth`` and ``allow_delegated_authorization`` cannot
        be ``None`` (the server answers ``InvalidInput``); to clear
        ``description`` pass ``""``. ``runtime_input_schema``,
        ``runtime_bindings`` and ``signed_uploads`` are cleared by passing
        ``None``. Mappings and sequences are copied into the request body.

        ``headers`` and ``auth`` replace the stored value as a whole, and
        ``{}`` clears them. To change one entry, start from the value you
        read, for example
        ``headers={**(server.headers or {}), "X-Trace": "1"}``. A header
        value or a secret ``auth`` field of ``"********"`` (as ``get()``
        returns it) keeps the stored value.

        Whether you may change the connector depends on whether you have the
        right to edit it. Changing ``url``, ``auth.issuer`` or
        ``auth.resource_metadata_url`` raises ``ConnectorNotEditable``
        (``authorized_by_others``) while other accounts hold authorizations of
        this connector that the change would keep; make that change in the
        web app. Otherwise the change is made, and the authorizations your
        team members made for themselves are removed: they must authorize
        again. A rename that collides with another connector of yours
        raises ``ConnectorNameConflict`` whose ``details["connector_id"]``
        is that other connector; it does not mean the change was made.

        Passing nothing returns the current state of a remote connector you
        are linked to, even without the right to edit it; a team-shared,
        catalog or non-remote connector raises the same error as for a real
        change.

        Sending the same arguments again gives the same result, so a retry
        after a timeout is safe. A ``ConnectorNotFound`` can still arrive
        after the change was stored, when another call removed your link to
        the connector in the meantime.

        Raises:
            TypeError: ``server_id`` is not an integer; raised before any
                request is sent.
            ConnectorNotFound: the key holder cannot see a connector with
                this id.
            ConnectorForbidden: the connector reaches the holder only
                through their team (``team_shared``), or the holder may not
                change it (``not_permitted``).
            ConnectorNotEditable: the connector is a catalog app connection
                (``catalog``), or other accounts hold authorizations the
                change would keep (``authorized_by_others``); see
                ``details["reason"]``.
            ConnectorNameConflict: the new ``name`` is taken.
            TransportNotSupported: the new ``transport``, or the
                connector's stored one, is not ``streamable_http`` or ``sse``.
            InvalidInput: the server rejected a value (``http_status`` 400
                or 422).
            InvalidAPIKey: the key is not accepted by the connector endpoints.
            MalformedResponse: the response status is not 200, the body is
                not JSON, or it is not a connector object. The change may
                already be stored; repeating the call is safe.
        """
        path = _server_path(server_id)
        body: dict[str, Any] = {}
        for key, value in (
            ("name", name),
            ("transport", transport),
            ("url", url),
            ("description", description),
            ("allow_delegated_authorization", allow_delegated_authorization),
        ):
            if value is not _UNSET:
                body[key] = value
        for key, mapping in (
            ("headers", headers),
            ("auth", auth),
            ("runtime_input_schema", runtime_input_schema),
        ):
            if mapping is not _UNSET:
                body[key] = None if mapping is None else dict(mapping)
        for key, sequence in (
            ("runtime_bindings", runtime_bindings),
            ("signed_uploads", signed_uploads),
        ):
            if sequence is not _UNSET:
                body[key] = None if sequence is None else [dict(x) for x in sequence]
        resp = self._client._request("PATCH", path, json=body)
        return _parse_mcp_server_write(
            _json_body(resp, "connector update"), "connector update"
        )

    def delete(self, server_id: int) -> None:
        """``DELETE /v1/mcp-servers/{server_id}`` -- delete your link to a connector.

        Returns ``None`` once the server has confirmed the deletion. The
        response body is checked: a 200 that is not the server's
        ``{"id": ...}`` confirmation (for example a proxy's HTML page)
        raises ``MalformedResponse`` instead of being reported as done.

        This removes the key holder's own connector. When the holder's team
        still shares it, the connector stays in ``list()`` with
        ``access="team"``, and deleting it again raises ``ConnectorForbidden``
        (``team_shared``); otherwise a second delete raises
        ``ConnectorNotFound``. A connector owned by the team can only be
        deleted by a team administrator.

        Raises:
            TypeError: ``server_id`` is not an integer; raised before any
                request is sent.
            ConnectorNotFound: the key holder cannot see a connector with
                this id (including one already deleted).
            ConnectorNotEditable: the connector is a catalog app connection
                (``catalog``), or another account manages its OAuth
                connection (``managed_elsewhere``); see
                ``details["reason"]``.
            ConnectorForbidden: ``details["reason"]`` is ``team_shared`` when
                the connector reaches the holder only through their team
                (including a repeated delete while the team still shares
                it), or ``not_permitted`` when a connector owned by the team
                is deleted by someone who is not a team administrator.
            InvalidAPIKey: the key is not accepted by the connector endpoints.
            MalformedResponse: the response status is not 200, the body is
                not JSON, or it is not a deletion confirmation. The delete
                may already have taken effect; a repeated call then raises
                ``ConnectorNotFound`` (or ``ConnectorForbidden`` while the
                team still shares the connector).
        """
        path = _server_path(server_id)
        resp = self._client._request("DELETE", path)
        _parse_mcp_server_delete(_json_body(resp, "connector deletion"))

    def set_enabled(self, server_id: int, enabled: bool) -> McpServer:
        """``PUT /v1/mcp-servers/{server_id}/enabled`` -- switch a connector on or off.

        Changes only the key holder's own switch. Setting the value it
        already has is not an error. It also works for catalog connectors.
        ``enabled`` is sent as given; the server accepts only a JSON boolean
        and answers ``InvalidInput`` otherwise. A switched-off connector
        stays in ``list()``, and ``list_tools()`` raises
        ``ConnectorUnavailable`` (``disabled``) for it.

        Raises:
            TypeError: ``server_id`` is not an integer; raised before any
                request is sent.
            ConnectorNotFound: the key holder cannot see a connector with
                this id. The switch may already have been changed when
                another call removed the holder's link in the meantime.
            ConnectorForbidden: the connector reaches the holder only
                through their team (``team_shared``).
            InvalidInput: ``enabled`` is not a boolean.
            InvalidAPIKey: the key is not accepted by the connector endpoints.
            MalformedResponse: the response status is not 200, the body is
                not JSON, or it is not a connector object. The switch may
                already have been changed; repeating the call is safe.
        """
        path = _server_path(server_id) + "/enabled"
        resp = self._client._request("PUT", path, json={"enabled": enabled})
        return _parse_mcp_server_write(
            _json_body(resp, "connector switch"), "connector switch"
        )
