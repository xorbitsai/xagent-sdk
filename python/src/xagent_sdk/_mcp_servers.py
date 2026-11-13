"""The ``client.mcp_servers`` namespace exposed on ``UserClient``.

An MCP connector (an "MCP server" in the API) gives agents tools from an
external MCP endpoint. This namespace lets the personal-key holder read
the connectors they can see.

This module is internal -- consumers reach it via ``user_client.mcp_servers``
-- so the class is exported via the user client and not re-exported from
``xagent_sdk.__init__``.
"""

import operator
from typing import TYPE_CHECKING
from urllib.parse import quote

from xagent_sdk._base import _json_body
from xagent_sdk.types import McpServer, _parse_mcp_server_list, _parse_mcp_server_read

if TYPE_CHECKING:
    from xagent_sdk.user_client import UserClient

# ``McpServersAPI.list()`` shadows the ``list`` builtin inside the class body,
# so a ``list[...]`` annotation on a later method cannot resolve to the
# builtin type. Reference it through a module-scope alias instead.
_McpServerList = list[McpServer]


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
    shares with them. A team connector is read-only for the holder; its
    ``access`` is ``"team"``.

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
