"""The ``client.agents`` namespace exposed on ``UserClient``.

Provides agent lifecycle management for the personal-key authenticated
caller: list the user's agents, create new ones (structured or from a
template), rotate the runtime API key on an existing agent, list or revoke
an agent's runtime keys, read or replace an agent's skill list, read or
replace an agent's connector selection, read or change an agent's
configuration, and list the models its slots can take.

``create()`` and ``create_from_template()`` default to
``generate_runtime_key=True`` to match the backend default; the returned
``AgentCreateResult.runtime_full_key`` is a **one-time** payload. The SDK
deliberately does not cache it -- the only chance to read the secret is
the immediate response. Pass ``generate_runtime_key=False`` when the
caller plans to rotate later via ``rotate_key()``.
"""

import operator
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from xagent_sdk._base import _json_body
from xagent_sdk._mcp_servers import _UNSET, _Unset
from xagent_sdk.errors import MalformedResponse
from xagent_sdk.types import (
    AgentConnectors,
    AgentCreateResult,
    AgentDetail,
    AgentSkillsUpdate,
    AgentSummary,
    ModelForAgent,
    RevokeKeyResult,
    RotateKeyResult,
    RuntimeKeyInfo,
    _parse_agent_connectors_read,
    _parse_agent_connectors_write,
    _parse_agent_create,
    _parse_agent_detail,
    _parse_agent_list,
    _parse_agent_skills,
    _parse_agent_skills_update,
    _parse_agent_write,
    _parse_model_for_agent_list,
    _parse_revoke_key,
    _parse_rotate_key,
    _parse_runtime_key_list,
)

if TYPE_CHECKING:
    from xagent_sdk.user_client import UserClient

# ``AgentsAPI.list()`` shadows the ``list`` builtin inside the class body, so
# a ``list[...]`` annotation on a later method cannot resolve to the builtin
# type. Reference it through a module-scope alias instead.
_StrList = list[str]
_KeyList = list[RuntimeKeyInfo]
_ModelForAgentList = list[ModelForAgent]


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


def _agent_path(agent_id: int) -> str:
    """The URL path of one agent, for ``get()``, ``update()``,
    ``list_assignable_models()`` and the connector methods.

    ``agent_id`` must be an integer: ``quote()`` leaves ``"."`` and ``".."``
    as they are and the HTTP client folds them away (``".."`` would reach
    ``/v1/connectors``), so anything that is not an integer raises
    ``TypeError`` before a request is sent.
    """
    return f"/v1/agents/{quote(str(operator.index(agent_id)), safe='')}"


class AgentsAPI:
    """The ``user_client.agents`` namespace."""

    def __init__(self, client: "UserClient") -> None:
        self._client = client

    def list(self) -> list[AgentSummary]:
        """``GET /v1/agents`` -- list the agents the key holder can see.

        Returns slim summaries (id + name + optional status). The backend
        sends a bare JSON array; a JSON body that is not a list returns an
        empty list. A body that is not JSON, or a status other than 200,
        raises ``MalformedResponse``. Standard error mapping applies
        (``InvalidAPIKey``, etc.).
        """
        resp = self._client._request("GET", "/v1/agents")
        return _parse_agent_list(_json_body(resp, "agent list"))

    def get(self, agent_id: int) -> AgentDetail:
        """``GET /v1/agents/{agent_id}`` -- read one agent's full configuration.

        Returns the agent's full configuration for any agent the key holder
        can see, including a team agent; ``agents.list()`` returns only the
        short summaries. The result's ``agent_id`` and ``owner_user_id`` are
        the wire fields ``id`` and ``user_id``; see ``AgentDetail``.

        Parsing is strict because the result is usually read, changed in one
        place and sent back: a body that is not the expected object, or that
        lacks any field or has one that cannot be read as its declared type,
        raises ``MalformedResponse`` and is never read as an empty value.
        ``AgentDetail.models`` is the one exception for its values, which are
        kept as the server stores them.

        Args:
            agent_id: An agent the key holder can see. Must be an ``int``
                (``True`` and ``False`` count as 1 and 0); anything else
                raises ``TypeError`` before a request is sent. The server
                answers a number outside the positive 32-bit range with
                ``InvalidInput``.

        Raises:
            TypeError: ``agent_id`` is not an integer; raised before any
                request is sent.
            InvalidAPIKey: the key is missing, is not a personal key, or is
                revoked (HTTP 401).
            InvalidInput: ``agent_id`` is 0, negative or 2**31 or more
                (HTTP 422).
            AgentNotFound: the key holder cannot see this agent (HTTP 404).
            InternalError: a server error (HTTP 500); a response without the
                V1 error envelope, carrying its own status (for example a 503
                while the server starts, a 502 page from a reverse proxy, or
                a 404 from a server without this route, which is not
                ``AgentNotFound``).
            XAgentTransportError: the request did not complete.
            MalformedResponse: the response status is not 200, the body is not
                JSON, or it is not an agent object.
        """
        resp = self._client._request("GET", _agent_path(agent_id))
        return _parse_agent_detail(_json_body(resp, "agent detail"))

    def update(
        self,
        agent_id: int,
        *,
        name: str | _Unset = _UNSET,
        description: str | _Unset = _UNSET,
        instructions: str | _Unset = _UNSET,
        models: Mapping[str, int | None] | _Unset = _UNSET,
    ) -> AgentDetail:
        """``PATCH /v1/agents/{agent_id}`` -- change an agent's name,
        description, instructions or model slots.

        Only the arguments you pass are sent; an argument you leave out keeps
        its stored value. Passing nothing sends ``{}``, changes nothing and
        returns the current configuration. ``name``, ``description`` and
        ``instructions`` cannot be ``None``: the type hints exclude it, and a
        ``None`` passed anyway is sent as ``null`` and refused (HTTP 400); to
        empty ``description`` or ``instructions`` pass ``""``. A value equal
        to the stored one changes nothing.

        ``models`` is merged slot by slot: only the slots in the mapping
        change (``general``, ``small_fast``, ``visual`` and ``compact``), the
        others keep their stored value. A slot set to an ``int`` takes that
        model id; a slot set to ``None`` clears it, so a task falls back to
        the model of the task itself or to the default model, which can
        differ with whoever starts the task. ``{}`` changes nothing. The
        mapping is copied into the request body.

        A slot sent with the model it already stores is left as it is and is
        not checked, so it is not reported in the error below even if that
        model is no longer usable. Any other slot can be set only to a model
        that ``list_assignable_models()`` lists with ``owner_can_use`` set to
        ``True``. The ``AgentDetail.models`` value ``get()`` returns cannot
        always be passed back as is: it is ``None`` for an agent without
        models, which raises ``TypeError`` before any request, and a value
        saved in the web app can hold keys other than the four slots, or
        slot values other than ``None`` or an integer from 1 to 2**31 - 1,
        which the server refuses with ``InvalidInput`` (HTTP 422).

        The new values apply to tasks started after the call returns. A task
        that is already running can keep using the previous values for a
        while: the server keeps the agent's settings cached per task, and how
        long depends on the task's state and on the server process. If the
        agent is the manager or an enabled worker of a workforce and its
        ``instructions`` or ``models`` changed, an ongoing workforce
        conversation can be rejected with ``workforce_config_changed`` on its
        next turn (a preview conversation in the workforce builder is not
        checked, and neither is a conversation that started before the
        check). The result is the agent as stored after the change, read as in
        ``get()``. Nothing checks that the agent is unchanged since you read
        it: of two concurrent writers, the later one overwrites the earlier
        one's value for the same field or slot. To change one slot without a
        read, pass only that slot.

        A task resolves each model slot as the user it runs as. If that user
        cannot use the model, a task started on this agent falls back to a
        default model for that slot without an error; when another agent
        calls this agent, such a ``general`` model fails the call and such a
        model in another slot is left unused. For an agent your team shares,
        prefer models that are shared with the team.
        ``AgentDetail.unresolved_models`` of ``get()`` lists the slots that
        fall back for you.

        Raises:
            TypeError: ``agent_id`` is not an integer, or ``models`` is
                ``None``; raised before any request is sent.
            InvalidAPIKey: the key is missing, is not a personal key, or is
                revoked (HTTP 401).
            AgentNotFound: the key holder cannot see this agent (HTTP 404).
                The same error comes back if the change was stored but the
                key holder can no longer see the agent when the server reads
                it again: the change took effect although the call raised.
            InvalidInput: the server rejected the request and nothing was
                changed; ``http_status`` says which kind.

                HTTP 422: a value is malformed (an empty ``name``, a
                ``name`` over 200 characters, text containing a NUL
                character, a ``description`` or ``instructions`` over
                1000000 characters, a model id that is zero,
                negative, a ``bool`` or over 2**31 - 1, an unknown slot name,
                a value the server cannot read as the expected type), or
                ``agent_id`` is out of range.

                HTTP 400: ``name``, ``description`` or ``instructions`` is
                ``None``; ``name`` is already used by another agent; or
                ``description`` or ``instructions`` is longer than 10000
                characters (up to 1000000) and also longer than the value
                stored now (a
                stored value that is already longer can be kept or
                shortened). A request refused for one of these is not
                checked for its model slots.
            ModelNotAssignable: a model slot cannot take the requested model
                (HTTP 400) and nothing was changed. ``details["models"]`` is
                a list with one entry for each refused slot, in the order
                ``general``, ``small_fast``, ``visual``, ``compact``:
                ``{"slot": str, "id": int, "reason": str}``, where ``id`` is
                the requested model's id. ``reason`` is one of:

                - ``"unavailable"``: you cannot use the model (it does not
                  exist, is switched off, or is not visible to you).
                - ``"not_llm"``: you can use it, but it is not an ``llm``
                  model.
                - ``"owner_cannot_use"``: you can use it, but the agent's
                  owner cannot; this entry also carries ``owner_user_id``,
                  the owner's user id.

                Treat any other string as ``"unavailable"``. A slot whose
                value did not change is never listed.
            InternalError: a server error (HTTP 500); a response without the
                V1 error envelope, carrying its own status. The change may
                or may not have been stored; call ``get()`` to see.
            XAgentTransportError: the request did not complete. The change
                may or may not have been stored; call ``get()`` to see.
            MalformedResponse: the response status is not 200, the body is
                not JSON, or it is not an agent object. The change may
                already be stored; repeating the call is safe (it declares
                the same values again), or call ``get()`` to see what is
                stored.
        """
        path = _agent_path(agent_id)
        body: dict[str, Any] = {}
        for key, value in (
            ("name", name),
            ("description", description),
            ("instructions", instructions),
        ):
            if value is not _UNSET:
                body[key] = value
        if models is not _UNSET:
            body["models"] = dict(models)
        resp = self._client._request("PATCH", path, json=body)
        return _parse_agent_write(_json_body(resp, "agent update"))

    def list_assignable_models(self, agent_id: int) -> _ModelForAgentList:
        """``GET /v1/agents/{agent_id}/assignable-models`` -- list the models
        you can put in this agent's model slots.

        Returns every ``llm`` model the key holder can use, each as a
        ``ModelForAgent``: the ``ModelInfo`` fields plus ``owner_can_use``,
        which says whether the agent's owner can use the model too. A model
        the owner cannot use is listed with ``owner_can_use=False``, not left
        out; ``update(models=...)`` can set a slot only to a model listed
        with ``owner_can_use=True``. A model the owner can use but the key
        holder cannot is not listed. An empty list means the server answered
        that the key holder can use no ``llm`` model. The entries come in
        the order the server sends them (the server does not guarantee an
        order).

        A model's ``id`` is specific to the deployment you call. To find a
        model by its ``model_id`` string here, pick the entry whose
        ``model_id`` matches and use its ``id``.

        Parsing is strict: if any entry does not match the expected shape,
        including an ``owner_can_use`` that is missing or cannot be read as a
        bool, the whole call raises ``MalformedResponse`` rather than
        returning a partial list. A body that is not a JSON array also
        raises it; it is never read as an empty list.

        Args:
            agent_id: An agent the key holder can see. Must be an ``int``
                (``True`` and ``False`` count as 1 and 0); anything else
                raises ``TypeError`` before a request is sent. The server
                answers a number outside the positive 32-bit range with
                ``InvalidInput``.

        Raises:
            TypeError: ``agent_id`` is not an integer; raised before any
                request is sent.
            InvalidAPIKey: the key is missing, is not a personal key, or is
                revoked (HTTP 401).
            InvalidInput: ``agent_id`` is 0, negative or 2**31 or more
                (HTTP 422).
            AgentNotFound: the key holder cannot see this agent (HTTP 404).
            InternalError: a server error (HTTP 500); a response without the
                V1 error envelope, carrying its own status (for example a 503
                while the server starts, a 502 page from a reverse proxy, or
                a 404 from a server without this route, which is not
                ``AgentNotFound``).
            XAgentTransportError: the request did not complete.
            MalformedResponse: the response status is not 200, the body is not
                JSON, or it is not an array of models that each carry
                ``owner_can_use``.
        """
        resp = self._client._request(
            "GET", f"{_agent_path(agent_id)}/assignable-models"
        )
        return _parse_model_for_agent_list(_json_body(resp, "assignable models"))

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
            _parse_agent_create(_json_body(resp, "agent creation")),
            generate_runtime_key,
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
            _parse_agent_create(_json_body(resp, "agent creation from template")),
            generate_runtime_key,
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
        return _parse_rotate_key(_json_body(resp, "runtime key rotation"))

    def list_keys(self, agent_id: int, *, include_revoked: bool = False) -> _KeyList:
        """``GET /v1/agents/{agent_id}/api-keys`` -- list an agent's runtime keys.

        Returns the keys newest first, without their secrets. Revoked keys are
        left out unless ``include_revoked=True``; paused keys are always
        listed. An empty list means the server answered that the agent has no
        matching key. Parsing is strict: a body that is not the expected
        array raises ``MalformedResponse`` and is never read as an empty list.

        Args:
            agent_id: An agent the key holder can manage. Pass an ``int``:
                the SDK does not check it, only percent-encodes it into the
                path.
            include_revoked: Also return revoked keys.

        Raises:
            AgentNotFound: the caller cannot manage this agent.
            InvalidInput: ``agent_id`` is out of range (HTTP 422).
            InternalError: a server without this route answers 404
                (``http_status=404``), which is not ``AgentNotFound``.
            InvalidAPIKey: the key is not accepted by this endpoint.
            MalformedResponse: the response status is not 200, the body is not
                JSON, or it is not a runtime key list.
        """
        path = f"/v1/agents/{quote(str(agent_id), safe='')}/api-keys"
        if include_revoked:
            path += "?include_revoked=true"
        resp = self._client._request("GET", path)
        return _parse_runtime_key_list(_json_body(resp, "runtime key list"))

    def revoke_key(self, agent_id: int, key_id: int) -> RevokeKeyResult:
        """``DELETE /v1/agents/{agent_id}/api-keys/{key_id}`` -- revoke one
        runtime key.

        Only the named key stops working; the agent's other keys are left
        alone. A task the key already started keeps running, but any later
        request made with the key is refused with ``InvalidAPIKey`` and an
        event stream opened with it is closed. No API call reverses a
        revocation. Calling this again for the same key is safe: it returns
        ``revoked=False`` with the original ``revoked_at``.

        One exception: while a key rotation is still settling, the keys it
        replaced are already revoked, so revoking one of them here changes
        nothing (``revoked=False``) and does not stop the server from
        restoring that key if the rotation's new key cannot be delivered.
        Once the rotation has settled, list the keys again and revoke the
        key if it is active.

        Args:
            agent_id: An agent the key holder can manage.
            key_id: ``RuntimeKeyInfo.key_id`` from ``list_keys()``.

        Raises:
            AgentNotFound: the caller cannot manage this agent.
            APIKeyNotFound: the agent has no key with this id.
            InvalidInput: an id is out of range (HTTP 422).
            InternalError: a server without this route answers 404 or 405,
                which is neither ``AgentNotFound`` nor ``APIKeyNotFound``.
            InvalidAPIKey: the key is not accepted by this endpoint.
            MalformedResponse: the server accepted the call but the body is
                not the expected object. Calling again is safe.
        """
        resp = self._client._request(
            "DELETE",
            f"/v1/agents/{quote(str(agent_id), safe='')}"
            f"/api-keys/{quote(str(key_id), safe='')}",
        )
        return _parse_revoke_key(_json_body(resp, "runtime key revocation"))

    def get_skills(self, agent_id: int) -> _StrList:
        """``GET /v1/agents/{agent_id}/skills`` -- read an agent's skill names.

        Returns the names in the agent's order. An empty list means the server
        answered that the agent has none. Parsing is strict because the result
        is usually edited and written back with ``replace_skills()``: a body
        that is not the expected object raises ``MalformedResponse`` and is
        never read as an empty list, which would clear the agent's list on the
        way back.

        Args:
            agent_id: An agent the key holder can see. Pass an ``int``: the SDK
                does not check it, only percent-encodes it into the path. The
                server answers a number outside the positive 32-bit range with
                ``InvalidInput``.

        Raises:
            AgentNotFound: the key holder cannot see this agent.
            InvalidInput: ``agent_id`` is out of range (HTTP 422).
            InternalError: a server without this route answers 404
                (``http_status=404``), which is not ``AgentNotFound``.
            InvalidAPIKey: the key is not accepted by this endpoint.
            MalformedResponse: the response status is not 200, the body is not
                JSON, or it is not an agent skill list object.
        """
        resp = self._client._request(
            "GET", f"/v1/agents/{quote(str(agent_id), safe='')}/skills"
        )
        return _parse_agent_skills(_json_body(resp, "agent skill list"))

    def replace_skills(self, agent_id: int, skills: _StrList) -> AgentSkillsUpdate:
        """``PUT /v1/agents/{agent_id}/skills`` -- replace an agent's skill names.

        ``skills`` becomes the agent's whole list, and ``[]`` clears it. Read
        the current list with ``get_skills()`` first and send it back with your
        change. There is no conflict detection: of two concurrent replacements
        the later one wins. Anyone who can see a team agent can replace its
        list, and the new list applies to everyone using the agent. A repeated
        name is kept once, at its first position. A list holds at most 100
        names, so an agent that already has more than 100 cannot be sent back
        with an edit; it can only be given a shorter list.

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
            InternalError: a server without this route answers 404 or 405,
                which is not ``AgentNotFound``.
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
            "PUT",
            f"/v1/agents/{quote(str(agent_id), safe='')}/skills",
            json={"skills": list(skills)},
        )
        return _parse_agent_skills_update(_json_body(resp, "agent skill update"))

    def get_connectors(self, agent_id: int) -> AgentConnectors:
        """``GET /v1/agents/{agent_id}/connectors`` -- read an agent's connectors.

        Returns the connector names stored on the agent, whether it also
        selects every MCP connector, and which of the names you cannot see.
        Parsing is strict because the result is usually edited and written
        back with ``replace_connectors()``: a body that is not the expected
        object, or that lacks any of its four fields, raises
        ``MalformedResponse`` and is never read as an empty selection, which
        would clear the agent's connectors on the way back.

        An agent that uses the default tool set reads as ``connectors=[]``
        with ``all_connectors=True``, the same as an agent that selects every
        MCP connector without naming any, so this result cannot tell the two
        apart. Writing to an agent on the default tool set raises
        ``AgentToolsUnconfigured``; the other one can be written to.

        Args:
            agent_id: An agent the key holder can see. Must be an ``int``
                (``True`` and ``False`` count as 1 and 0); anything else
                raises ``TypeError`` before a request is sent. The server
                answers a number outside the positive 32-bit range with
                ``InvalidInput``.

        Raises:
            TypeError: ``agent_id`` is not an integer; raised before any
                request is sent.
            InvalidAPIKey: the key is missing, is not a personal key, or is
                revoked (HTTP 401).
            InvalidInput: ``agent_id`` is 0, negative or 2**31 or more
                (HTTP 422).
            AgentNotFound: the key holder cannot see this agent (HTTP 404).
            InternalError: a server error (HTTP 500, for example when the
                names the key holder can see could not be read); a response
                without the V1 error envelope, carrying its own status (for
                example a 503 while the server starts, a 502 page from a
                reverse proxy, or a 404 from a server without this route,
                which is not ``AgentNotFound``).
            XAgentTransportError: the request did not complete.
            MalformedResponse: the response status is not 200, the body is not
                JSON, or it is not an agent connectors object.
        """
        resp = self._client._request("GET", f"{_agent_path(agent_id)}/connectors")
        return _parse_agent_connectors_read(_json_body(resp, "agent connectors"))

    def replace_connectors(
        self,
        agent_id: int,
        connectors: Sequence[str],
        *,
        all_connectors: bool,
    ) -> AgentConnectors:
        """``PUT /v1/agents/{agent_id}/connectors`` -- replace an agent's connectors.

        ``connectors`` becomes the agent's whole list of named connectors, and
        ``[]`` clears it. Read the current selection with ``get_connectors()``
        first and send it back with your change, passing the ``all_connectors``
        you read unless you mean to change it: ``all_connectors`` has no
        default because a wrong guess would silently change which connectors
        the agent can use. There is no conflict detection: of two concurrent
        replacements the later one wins.

        The names are stored in the order given, so a change of order alone is
        also a write. A repeated name is kept once, at its first position;
        ``"crm"`` and ``"CRM"`` are different names and both are kept. A name
        is a connector or a Custom API; ``mcp_servers.list()`` does not list
        Custom APIs. ``all_connectors=True`` selects every MCP connector, but a
        Custom API is selected only by being named in ``connectors``.

        A name that is not on the agent yet must be one the key holder can
        see; otherwise the call is refused with ``InvalidInput`` (HTTP 400, the
        message lists at most five). Whether a name can be seen is decided
        after stripping surrounding whitespace, lower-casing it and replacing
        each space and each hyphen with an underscore; the name itself is
        stored and read back as you gave it. A connector the holder has
        switched off can be added to a personal agent, but a task loads it
        only once it is switched on again. A connector that only the team
        shares is accepted too, but a task of a personal agent does not load
        it. Names already on the agent are kept as they are even if the holder
        cannot see them (the result lists them in ``unresolved``); only the
        names this call adds are checked for visibility.

        A team agent is also checked by a validator the deployment installs.
        It checks the whole new list, including ``all_connectors=True``
        itself, so it can refuse names that were already on the agent, and
        ``ConnectorNotShared.details["connectors"]`` can name connectors you
        did not send.

        Replacing the connectors moves the agent's connector entries to the
        end of its tool list and removes categories from that list that an
        agent may not have, so other parts of the tool list can change too.

        New tasks use the new selection at once. How long an existing task
        keeps using the previous one depends on the task's state. If the agent
        is the manager or an enabled worker of a workforce and its tool list
        now differs, other than in order, from the list it had when an ongoing
        workforce conversation started, that conversation is rejected with
        ``workforce_config_changed`` on its next turn; a preview conversation
        in the workforce builder is not checked. The SDK has no exception
        class for that code, so the next ``tasks.append()`` raises
        ``InternalError`` carrying it.

        A selection cannot be sent back unchanged when it has more than 100
        names, or when a name is empty, blank or longer than 100 characters;
        such an agent can only be given a selection that fits.

        Calling this again after a ``MalformedResponse`` or a timeout is
        allowed: the call declares the same selection once more. If nobody
        changed the agent in the meantime nothing is written; if someone did,
        their change is overwritten. For an agent that uses the default tool set
        the call is refused again with ``AgentToolsUnconfigured``.

        Args:
            agent_id: An agent the key holder can see. Must be an ``int``
                (``True`` and ``False`` count as 1 and 0); anything else
                raises ``TypeError`` before a request is sent.
            connectors: The connector names, in the order to store. A tuple
                or any other sequence of ``str`` is sent as a list; a single
                ``str`` raises ``TypeError``.
            all_connectors: Whether the agent also selects every MCP
                connector. Keyword-only and required.

        Raises:
            TypeError: ``agent_id`` is not an integer, or ``connectors`` is a
                single string, which would otherwise be sent as one name per
                character; raised before any request is sent.
            InvalidAPIKey: the key is missing, is not a personal key, or is
                revoked (HTTP 401).
            InvalidInput: a name that is not on the agent yet cannot be seen
                by the key holder (HTTP 400, the message lists at most five);
                or the body is not acceptable (HTTP 422: more than 100 names,
                a name that is empty, blank or longer than 100 characters, or
                a body of the wrong shape); or ``agent_id`` is 0, negative or
                2**31 or more (HTTP 422).
            AgentNotFound: the key holder cannot see this agent, or it was
                deleted while the call was being handled (HTTP 404).
            ConnectorNotShared: the agent's team cannot use the selection
                (HTTP 400); see its documentation for ``details``.
            AgentToolsUnconfigured: the agent uses the default tool set and
                has no selection of its own (HTTP 409). Choose its tools
                in the web app first.
            InternalError: a server error (HTTP 500, for example when the
                names the key holder can see could not be read, or the team's
                validator itself failed; nothing is written in either case);
                a response without the V1 error envelope, carrying its own
                status (for example a 503 while the server starts, a 502 page
                from a reverse proxy, or a 404 from a server without this
                route, which is not ``AgentNotFound``).
            XAgentTransportError: the request did not complete.
            MalformedResponse: the response status is not 200, the body is not
                JSON, or it is not an agent connectors object. The replacement
                may already have been applied.
        """
        if isinstance(connectors, str):
            raise TypeError("connectors must be a list of names, not a single string")
        path = f"{_agent_path(agent_id)}/connectors"
        resp = self._client._request(
            "PUT",
            path,
            json={"connectors": list(connectors), "all_connectors": all_connectors},
        )
        return _parse_agent_connectors_write(
            _json_body(resp, "agent connectors update")
        )
