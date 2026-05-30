"""The ``client.agents`` namespace exposed on ``UserClient``.

Provides agent lifecycle management for the personal-key authenticated
caller: list the user's agents, create new ones (structured or from a
template), and rotate the runtime API key on an existing agent.

``create()`` and ``create_from_template()`` default to
``generate_runtime_key=True`` to match the backend default; the returned
``AgentCreateResult.runtime_full_key`` is a **one-time** payload. The SDK
deliberately does not cache it -- the only chance to read the secret is
the immediate response. Pass ``generate_runtime_key=False`` when the
caller plans to rotate later via ``rotate_key()``.
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from xagent_sdk.errors import MalformedResponse
from xagent_sdk.types import (
    AgentCreateResult,
    AgentSummary,
    RotateKeyResult,
    _parse_agent_create,
    _parse_agent_list,
    _parse_rotate_key,
)

if TYPE_CHECKING:
    from xagent_sdk.user_client import UserClient


def _require_runtime_key(
    result: AgentCreateResult, generate_runtime_key: bool
) -> AgentCreateResult:
    """Fail closed when a key was requested but the response carried none.

    ``generate_runtime_key=True`` is a promise that the response includes
    a one-time runtime key. If the backend omits it, returning a result
    with ``runtime_full_key=None`` is dangerous: the caller is expected to
    do ``AgentClient(api_key=result.runtime_full_key)``, and ``None`` there
    falls back to ``XAGENT_API_KEY`` in the environment -- silently using
    a *different* agent's credential. Raise instead of handing back a
    keyless result that invites that fallback.
    """
    if generate_runtime_key and result.runtime_full_key is None:
        raise MalformedResponse(
            "malformed_response",
            "create requested generate_runtime_key=True but the response "
            "carried no runtime key; refusing to return a keyless result "
            "that would let AgentClient fall back to XAGENT_API_KEY",
            http_status=None,
        )
    return result


class AgentsAPI:
    """The ``user_client.agents`` namespace."""

    def __init__(self, client: "UserClient") -> None:
        self._client = client

    def list(self) -> list[AgentSummary]:
        """``GET /v1/agents`` -- list agents owned by the personal key's user.

        Returns slim summaries (id + name + optional status). Returns an
        empty list when the backend sends ``{"agents": []}`` or a non-dict
        body. Standard error mapping applies (``InvalidAPIKey``, etc.).
        """
        resp = self._client._request("GET", "/v1/agents")
        return _parse_agent_list(resp.json())

    def create(
        self,
        *,
        name: str,
        instructions: str,
        generate_runtime_key: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> AgentCreateResult:
        """``POST /v1/agents`` -- structured agent creation.

        Args:
            name: Display name shown in agent pickers; backend enforces
                uniqueness within the user's agent set per its own policy.
            instructions: System prompt / role description.
            generate_runtime_key: When ``True`` (default), the backend
                provisions a fresh runtime key in the same transaction
                and returns it via ``AgentCreateResult.runtime_full_key``.
                Set ``False`` when the caller intends to issue the first
                runtime key later via ``rotate_key()``.
            metadata: Free-form correlation data the backend persists
                without interpretation; analogous to ``tasks.create``'s
                ``metadata`` parameter. Omitted from the wire when None.

        Returns:
            ``AgentCreateResult`` with ``agent_id``, ``name``, and
            (when ``generate_runtime_key=True``) ``runtime_full_key`` +
            ``runtime_key_prefix``. ``runtime_full_key`` is one-time;
            persist to a secret vault and never log.

        Raises:
            InvalidInput: 422 -- backend rejected the body (e.g. empty
                ``name`` or ``instructions``).
            InvalidAPIKey: 401 -- personal key invalid / revoked.
            MalformedResponse: ``generate_runtime_key=True`` but the
                response carried no runtime key (fail closed rather than
                return a keyless result).
        """
        body: dict[str, Any] = {
            "name": name,
            "instructions": instructions,
            "generate_runtime_key": generate_runtime_key,
        }
        if metadata is not None:
            body["metadata"] = metadata
        resp = self._client._request("POST", "/v1/agents", json=body)
        return _require_runtime_key(
            _parse_agent_create(resp.json()), generate_runtime_key
        )

    def create_from_template(
        self,
        template_id: str,
        *,
        overrides: Mapping[str, Any] | None = None,
        generate_runtime_key: bool = True,
    ) -> AgentCreateResult:
        """``POST /v1/agents/from-template`` -- create an agent by template.

        The backend loads the template's ``agent_config`` and overlays
        any caller-supplied fields on top before persisting. ``overrides``
        keys (``name``, ``description``, ``instructions``,
        ``execution_mode``, ``models``, ``knowledge_bases``, ``skills``,
        ``tool_categories``, ``suggested_prompts``) are spread into the
        request body alongside ``template_id`` and ``generate_runtime_key``;
        unknown keys are dropped by the backend and have no effect.

        Args:
            template_id: Template identifier from
                ``templates.list()`` / ``templates.get()``.
            overrides: Optional dict of fields to override on the
                template (e.g. ``{"name": "My Bot"}``). Spread flat into
                the wire body; ``template_id`` and ``generate_runtime_key``
                always win over collisions.
            generate_runtime_key: Same semantics as ``create()``.

        Returns:
            ``AgentCreateResult``; see ``create()`` for field semantics.

        Raises:
            TemplateNotFound: 404 ``template_not_found`` -- unknown
                ``template_id``.
            InvalidInput: 422 -- overrides contain malformed values.
            InvalidAPIKey: 401 -- personal key invalid / revoked.
            MalformedResponse: ``generate_runtime_key=True`` but the
                response carried no runtime key (fail closed rather than
                return a keyless result).
        """
        body: dict[str, Any] = {
            **(dict(overrides) if overrides else {}),
            "template_id": template_id,
            "generate_runtime_key": generate_runtime_key,
        }
        resp = self._client._request("POST", "/v1/agents/from-template", json=body)
        return _require_runtime_key(
            _parse_agent_create(resp.json()), generate_runtime_key
        )

    def rotate_key(self, agent_id: int) -> RotateKeyResult:
        """``POST /v1/agents/{agent_id}/api-key`` -- rotate runtime key.

        Destructive: the previous runtime key for ``agent_id`` is
        revoked atomically with the new key insertion. The returned
        ``full_key`` is a **one-time** payload -- existing AgentClient
        instances using the old key will start receiving
        ``InvalidAPIKey`` on the next request.

        Args:
            agent_id: Target agent. Must be owned by the personal key's
                user.

        Returns:
            ``RotateKeyResult`` with ``full_key`` (one-time secret),
            ``key_prefix`` (public-safe handle), and ``created_at``.

        Raises:
            AgentNotFound: 404 ``agent_not_found`` -- agent does not
                exist or is not owned by the calling user.
            InvalidAPIKey: 401 -- personal key invalid / revoked.
        """
        resp = self._client._request("POST", f"/v1/agents/{agent_id}/api-key")
        return _parse_rotate_key(resp.json())
