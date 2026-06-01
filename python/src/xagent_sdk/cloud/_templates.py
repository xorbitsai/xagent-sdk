"""The ``workspace.templates`` namespace exposed on ``WorkspaceClient``.

Templates are server-managed presets a workspace can offer when creating
an agent. The list endpoint returns slim ``Template`` summaries; the
detail endpoint returns the full ``agent_config`` used by
``WorkspaceAgentsAPI.create_from_template``. Response shapes match the
personal-key template surface, so the shared parsers are reused.
"""

from typing import TYPE_CHECKING
from urllib.parse import quote

from xagent_sdk.types import (
    Template,
    TemplateDetail,
    _parse_template_detail,
    _parse_template_list,
)

if TYPE_CHECKING:
    from xagent_sdk.cloud.workspace_client import WorkspaceClient


class WorkspaceTemplatesAPI:
    """The ``workspace.templates`` namespace."""

    def __init__(self, client: "WorkspaceClient") -> None:
        self._client = client

    def list(self) -> list[Template]:
        """``GET /v1/workspace/templates`` -- list available templates.

        Returns a slim summary (id + name + optional description) per
        template. Returns an empty list for a non-list body. Standard
        error mapping applies.
        """
        resp = self._client._request("GET", "/v1/workspace/templates")
        return _parse_template_list(resp.json())

    def get(self, template_id: str) -> TemplateDetail:
        """``GET /v1/workspace/templates/{template_id}`` -- template detail.

        The returned ``TemplateDetail`` includes the ``agent_config`` dict
        whose shape the backend owns.

        Raises ``TemplateNotFound`` (404 ``template_not_found``) when the
        template does not exist.
        """
        # Encode the id as a single path segment so a value with "/", "?",
        # "#" or "%" cannot alter the route or leak into the query string.
        safe_id = quote(template_id, safe="")
        resp = self._client._request("GET", f"/v1/workspace/templates/{safe_id}")
        return _parse_template_detail(resp.json())
