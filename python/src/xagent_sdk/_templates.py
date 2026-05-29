"""The ``client.templates`` namespace exposed on ``UserClient``.

Templates are server-managed presets (Content Generator, Analyzer, Q&A,
Assistant, ...) that a SaaS app can offer to its users when they create
an agent without writing instructions from scratch. The list endpoint
returns slim ``Template`` summaries suitable for a picker UI; the detail
endpoint returns the full ``agent_config`` dict needed by
``AgentsAPI.create_from_template``.

This module is internal -- consumers reach it via
``user_client.templates`` -- so the class is exported via the user
client and not re-exported from ``xagent_sdk.__init__``.
"""

from typing import TYPE_CHECKING

from xagent_sdk.types import (
    Template,
    TemplateDetail,
    _parse_template_detail,
    _parse_template_list,
)

if TYPE_CHECKING:
    from xagent_sdk.user_client import UserClient


class TemplatesAPI:
    """The ``user_client.templates`` namespace."""

    def __init__(self, client: "UserClient") -> None:
        self._client = client

    def list(self) -> list[Template]:
        """``GET /v1/templates`` -- list available agent templates.

        Returns a slim summary (id + name + optional description) per
        template. Use ``get(template_id)`` to retrieve the full
        ``agent_config`` needed by ``agents.create_from_template``.

        Returns an empty list when the backend sends ``{"templates": []}``
        or a non-dict body; raises ``InvalidAPIKey`` / other ``XAgentError``
        subclasses for HTTP errors per the standard envelope mapping.
        """
        resp = self._client._request("GET", "/v1/templates")
        return _parse_template_list(resp.json())

    def get(self, template_id: str) -> TemplateDetail:
        """``GET /v1/templates/{template_id}`` -- fetch template detail.

        The returned ``TemplateDetail`` includes the ``agent_config`` dict
        whose shape the backend owns; ``create_from_template`` merges it
        with any caller-supplied overrides server-side.

        Raises ``TemplateNotFound`` (404 ``template_not_found``) when the
        backend reports the template does not exist.
        """
        resp = self._client._request("GET", f"/v1/templates/{template_id}")
        return _parse_template_detail(resp.json())
