"""The ``client.models`` namespace exposed on ``UserClient``.

Lists the models the personal-key holder can use. The ``id`` of a model is
what an agent's model slot stores.

This module is internal -- consumers reach it via ``user_client.models`` --
so the class is exported via the user client and not re-exported from
``xagent_sdk.__init__``.
"""

from typing import TYPE_CHECKING

from xagent_sdk._base import _json_body
from xagent_sdk.types import ModelInfo, _parse_model_list

if TYPE_CHECKING:
    from xagent_sdk.user_client import UserClient

# ``ModelsAPI.list()`` shadows the ``list`` builtin inside the class body, so
# a ``list[...]`` annotation on a later method could not resolve to the
# builtin type. Reference it through a module-scope alias instead.
_ModelList = list[ModelInfo]


class ModelsAPI:
    """The ``user_client.models`` namespace."""

    def __init__(self, client: "UserClient") -> None:
        self._client = client

    def list(self) -> _ModelList:
        """``GET /v1/models`` -- list the models the key holder can use.

        Returns every model the holder can use, of every category, in the
        order the server sends them (the server does not guarantee an
        order). The list is read from the server each time. An empty list
        means the server answered that the holder has no models.

        Only a model whose ``category`` is ``"llm"`` takes effect in an
        agent's model slot when a task runs. ``agents.create_from_template()``
        also accepts a model of another category in ``overrides["models"]``,
        and ``agents.get()`` then lists that slot in ``unresolved_models``.
        A model's ``id`` is specific to the deployment you call; use
        ``model_id`` to find the same model in another deployment.

        Parsing is strict: if any entry does not match the expected shape,
        the whole call raises ``MalformedResponse`` rather than returning a
        partial list. A body that is not a JSON array also raises it; it is
        never read as an empty list.

        Raises:
            InvalidAPIKey: the key is missing, is not a personal key, or is
                revoked (HTTP 401).
            InternalError: a server error (HTTP 500); a response without the
                V1 error envelope, carrying its own status (for example a 503
                while the server starts, a 502 page from a reverse proxy, or
                a 404 from a server without this route).
            XAgentTransportError: the request did not complete.
            MalformedResponse: the response status is not 200, the body is not
                JSON, or it is not an array of models.
        """
        resp = self._client._request("GET", "/v1/models")
        return _parse_model_list(_json_body(resp, "model list"))
