"""The ``client.agents`` namespace exposed on ``UserClient``.

Provides agent lifecycle management for the personal-key authenticated
caller: list the user's agents, create new ones (structured or from a
template), rotate the runtime API key on an existing agent, and read or
replace an agent's skill list.

``create()`` and ``create_from_template()`` default to
``generate_runtime_key=True`` to match the backend default; the returned
``AgentCreateResult.runtime_full_key`` is a **one-time** payload. The SDK
deliberately does not cache it -- the only chance to read the secret is
the immediate response. Pass ``generate_runtime_key=False`` when the
caller plans to rotate later via ``rotate_key()``.
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from xagent_sdk._base import _json_body
from xagent_sdk.errors import MalformedResponse
from xagent_sdk.types import (
    AgentCreateResult,
    AgentSkillsUpdate,
    AgentSummary,
    RotateKeyResult,
    _parse_agent_create,
    _parse_agent_list,
    _parse_agent_skills,
    _parse_agent_skills_update,
    _parse_rotate_key,
)

if TYPE_CHECKING:
    from xagent_sdk.user_client import UserClient

# ``AgentsAPI.list()`` shadows the ``list`` builtin inside the class body, so
# a ``list[...]`` annotation on a later method cannot resolve to the builtin
# type. Reference it through a module-scope alias instead.
_StrList = list[str]


def _require_runtime_key(
    result: AgentCreateResult, generate_runtime_key: bool
) -> AgentCreateResult:
    """Fail closed when a key was requested but the response lacks one.

    ``generate_runtime_key=True`` is a promise that the response carries a
    usable one-time runtime key. ``None`` means the backend omitted the
    key block, and ``""`` is still not a usable credential. Catch both at
    ``create`` time with an error that names the backend contract, rather
    than returning an apparently successful result that only fails when
    the caller later constructs an ``AgentClient``.
    """
    if generate_runtime_key and not result.runtime_full_key:
        raise MalformedResponse(
            "malformed_response",
            "create requested generate_runtime_key=True but the response "
            "carried no runtime key; refusing to return a keyless result",
            http_status=None,
        )
    return result


class AgentsAPI:
    """The ``user_client.agents`` namespace."""

    def __init__(self, client: "UserClient") -> None:
        self._client = client

    def list(self) -> list[AgentSummary]:
        """``GET /v1/agents`` -- list the agents the key holder can see.

        Returns slim summaries (id + name + optional status). The backend
        sends a bare JSON array; a non-list body returns an empty list.
        Standard error mapping applies (``InvalidAPIKey``, etc.).
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

    def get_skills(self, agent_id: int) -> _StrList:
        """``GET /v1/agents/{agent_id}/skills`` -- read an agent's skill names.

        Returns the names in the agent's order. An empty list means the server
        answered that the agent has none. Parsing is strict because the result
        is usually edited and written back with ``replace_skills()``: a body
        that is not the expected object raises ``MalformedResponse`` and is
        never read as an empty list, which would clear the agent's list on the
        way back.

        Args:
            agent_id: An agent the key holder can see. The SDK does not check
                it; the server rejects a value that is not a positive 32-bit
                integer.

        Raises:
            AgentNotFound: the key holder cannot see this agent.
            InvalidInput: ``agent_id`` is out of range (HTTP 422).
            InvalidAPIKey: the key is not accepted by this endpoint.
            MalformedResponse: the response status is not 200, the body is not
                JSON, or it is not an agent skill list object.
        """
        resp = self._client._request("GET", f"/v1/agents/{agent_id}/skills")
        return _parse_agent_skills(_json_body(resp, "agent skill list"))

    def replace_skills(self, agent_id: int, skills: _StrList) -> AgentSkillsUpdate:
        """``PUT /v1/agents/{agent_id}/skills`` -- replace an agent's skill names.

        ``skills`` becomes the agent's whole list, and ``[]`` clears it. Read
        the current list with ``get_skills()`` first and send it back with your
        change. There is no conflict detection: of two concurrent replacements
        the later one wins. A repeated name is kept once, at its first
        position. A list holds at most 100 names, so an agent that already has
        more than 100 cannot be sent back with an edit; it can only be given a
        shorter list.

        A name that is not on the agent yet must be the name of a skill the key
        holder can see, or a valid personal skill name (1-100 ASCII letters,
        digits, ``_`` or ``-``). Names already on the agent are kept as they
        are. ``unresolved_for_caller`` on the result lists the names this call
        added that the key holder cannot see. They are stored anyway, because a
        task loads the skills of the user who runs it, and that user may have a
        skill with this name.

        New tasks use the new list at once. An existing task keeps using the
        previous list while its cached agent runtime is alive: the cache is
        released after 120 seconds idle, or after 1800 seconds for a task that
        is paused or waiting for the user, and each server process has its own
        cache. If the agent is the manager or an enabled worker of a workforce
        and its list now differs, other than in order, from the list it had
        when an ongoing workforce conversation started, that conversation is
        rejected with ``workforce_config_changed`` on its next turn.

        Raises:
            TypeError: ``skills`` is a single string, which would otherwise be
                sent as one name per character; raised before any request is
                sent.
            AgentNotFound: the key holder cannot see this agent.
            InvalidInput: a new name is neither visible nor valid (HTTP 400;
                the message lists at most five), or the list has more than 100
                names, a name is outside 1-1000 characters, or ``agent_id`` is
                out of range (HTTP 422).
            InvalidAPIKey: the key is not accepted by this endpoint.
            MalformedResponse: the response status is not 200, the body is not
                JSON, or it is not an agent skill update object. The replacement
                may already have been applied; call ``get_skills()`` to check
                what the server stored.
        """
        if isinstance(skills, str):
            raise TypeError("skills must be a list of names, not a single string")
        resp = self._client._request(
            "PUT", f"/v1/agents/{agent_id}/skills", json={"skills": list(skills)}
        )
        return _parse_agent_skills_update(_json_body(resp, "agent skill update"))
