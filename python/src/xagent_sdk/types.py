from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import TypeAdapter

from xagent_sdk.errors import MalformedResponse


class TaskStatus(StrEnum):
    """Lifecycle states a task can hold.

    ``run()``/``wait()`` stop polling and return on a terminal state
    (``COMPLETED``/``FAILED``) or on ``WAITING_FOR_USER`` -- the latter
    blocks on this caller answering the task's pending question via
    ``reply()``, so a passive poller would never see it advance. Inspect
    ``TaskInfo.pending_interaction`` for the question text before
    answering. ``PAUSED`` is non-terminal and keeps the loop going: the
    backend allows ``append()`` onto a paused task, transitioning it back
    to ``RUNNING``, and another caller may drive that, so waiting through
    it lets an observer see the transition.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    WAITING_FOR_USER = "waiting_for_user"


class StepType(StrEnum):
    """Public timeline step types.

    Backend collapses ~32 internal trace event types into these 4 stable
    surface values. The v1 SDK treats this as a closed public contract;
    adding a new public step type requires a coordinated SDK update.
    Type-specific ``data`` dictionaries may still grow new keys over time,
    so callers should ignore unknown keys inside ``Step.data``.
    """

    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    AGENT_DELEGATION = "agent_delegation"
    MESSAGE = "message"


@dataclass(frozen=True)
class UserPrincipal:
    """``GET /v1/me`` payload -- the user identity the personal key
    is bound to.

    ``principal_type`` is the stable enum string ``"user"`` for the
    surface ``UserClient`` covers. ``username`` is the user's login name.
    ``email`` is optional and ``None`` when the account has no email set.
    ``key_prefix`` is the public-safe 6-char handle
    (``xag_personal_<prefix>_...``) and is safe to log; the secret half of
    the key is never returned by this endpoint.
    """

    principal_type: str
    user_id: int
    username: str
    email: str | None
    key_prefix: str


@dataclass(frozen=True)
class Template:
    """``GET /v1/templates`` list entry.

    The list endpoint returns a slim summary suitable for showing in a
    SaaS-app picker; the full ``agent_config`` (system prompt, tools,
    model defaults) is only included in the per-template
    ``GET /v1/templates/{template_id}`` response (``TemplateDetail``).
    """

    template_id: str
    name: str
    description: str | None = None


@dataclass(frozen=True)
class TemplateDetail:
    """``GET /v1/templates/{template_id}`` payload.

    Extends ``Template`` with ``agent_config``, the dict that
    ``UserClient.agents.create_from_template()`` merges with the caller's
    ``overrides`` before posting to the backend. The shape of
    ``agent_config`` is intentionally typed as ``dict[str, Any]``: it is
    the backend's evolving template-config envelope (currently includes
    things like ``instructions`` / ``mode`` / ``tools_default``), and
    pinning a strict schema here would force SDK releases for every
    backend template-config change.
    """

    template_id: str
    name: str
    description: str | None
    agent_config: dict[str, Any]


@dataclass(frozen=True)
class AgentSummary:
    """``GET /v1/agents`` list entry.

    Slim shape for listing agents owned by the personal key's user.
    The optional ``status`` reflects the agent's published-state
    (e.g. ``"active"`` / ``"draft"`` / ``"paused"``); ``None`` means the
    backend omitted the field on this entry.
    """

    agent_id: int
    name: str
    status: str | None = None


@dataclass(frozen=True)
class AgentCreateResult:
    """``POST /v1/agents`` / ``POST /v1/agents/from-template`` payload.

    Carries the new agent's identity plus the **one-time** runtime key
    when ``generate_runtime_key=True`` was passed (the backend default).
    ``runtime_full_key`` is ``None`` only when the caller explicitly set
    ``generate_runtime_key=False`` and intends to call ``rotate_key()``
    later to materialize the first runtime key.

    The ``runtime_full_key`` is the only chance the SDK or its caller
    have to read the secret in cleartext; store it in a secret vault
    immediately and never log it. ``runtime_key_prefix`` is the
    public-safe 6-char lookup handle and is safe to log for tracing.
    """

    agent_id: int
    name: str
    runtime_full_key: str | None
    runtime_key_prefix: str | None


@dataclass(frozen=True)
class RotateKeyResult:
    """``POST /v1/agents/{agent_id}/api-key`` payload.

    Rotation is destructive: the previous runtime key for this agent is
    revoked atomically with the new key insertion. The ``full_key`` is a
    one-time payload exposed only on this response; storing it is the
    caller's responsibility.
    """

    full_key: str
    key_prefix: str
    created_at: datetime


@dataclass(frozen=True)
class CreateTaskResult:
    """``POST /v1/chat/tasks`` 202 payload.

    ``status`` is always ``TaskStatus.PENDING`` at this point; observe
    transitions via ``client.tasks.get()`` or ``client.tasks.wait()``.
    """

    task_id: int
    agent_id: int
    status: TaskStatus
    created_at: datetime


@dataclass(frozen=True)
class AppendResult:
    """``POST /v1/chat/tasks/{id}/messages`` 202 payload.

    Also the return type of ``client.tasks.reply()``
    (``POST /v1/chat/tasks/{id}/reply``): the two endpoints return the
    same shape with the same field semantics, so the SDK does not carry
    a separate ``ReplyResult`` for it. ``status`` is ``TaskStatus.RUNNING``
    -- the backend's atomic claim has already flipped the task row by the
    time the response is built. The timestamp field is ``accepted_at``
    (when the server scheduled execution), not ``created_at``.
    """

    task_id: int
    agent_id: int
    status: TaskStatus
    accepted_at: datetime


@dataclass(frozen=True)
class PendingInteraction:
    """The task's current unanswered question.

    Present on ``TaskInfo.pending_interaction`` only while ``status`` is
    ``WAITING_FOR_USER`` and the task has at least one persisted question
    turn; ``None`` otherwise, including on a ``WAITING_FOR_USER`` task
    that has not (yet, or ever) recorded a question -- reply to such a
    task with ``reply()`` is still valid even though there is nothing to
    show here.

    ``question`` is the assistant's prompt text. The server strips it
    before persisting and never records a question row that is empty
    after stripping, so in the current backend this is always a
    non-empty string when this object exists -- that is a server-side
    behavior this class documents, not something the SDK itself
    validates. ``interactions`` is an opaque list of
    structured-input descriptors (e.g. a select box or file upload) that
    the agent tool which raised the question attached to it, or ``None``
    when the question came with no such descriptors. ``[]`` and ``None``
    carry the same meaning -- "no structured control, answer in plain
    text" -- the server does not normalize between them and neither does
    the SDK; treat both the same way. Each descriptor's shape is defined
    by the agent tool that raised the question, not by this SDK, so it
    stays untyped (``dict[str, Any]``) rather than a closed schema that
    would force an SDK release for every new descriptor field.

    Answer with ``client.tasks.reply()``, not ``append()`` -- ``append()``
    raises ``InteractionResponseRequired`` on a ``WAITING_FOR_USER`` task.
    """

    question: str
    interactions: list[dict[str, Any]] | None


@dataclass(frozen=True)
class TaskInfo:
    """``GET /v1/chat/tasks/{id}`` payload -- snapshot of one task row.

    ``input`` / ``output`` reflect the latest turn only; full transcript
    history is reconstructed from ``client.tasks.steps()`` by filtering
    ``step.type == StepType.MESSAGE``.

    ``pending_interaction`` carries the task's unanswered question while
    ``status`` is ``WAITING_FOR_USER``; ``None`` for every other status,
    and also ``None`` when talking to a backend that predates this field
    -- the default value means an old server's response (which omits the
    key entirely) parses the same as a new server's ``null``.
    """

    task_id: int
    agent_id: int
    status: TaskStatus
    input: str | None
    output: str | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None
    pending_interaction: PendingInteraction | None = None


@dataclass(frozen=True)
class Step:
    """One step on the public agent timeline.

    Type-specific ``data`` keys (kept as untyped ``dict[str, Any]`` because
    tool args / results carry arbitrary JSON):

      - ``StepType.THINKING``         -- ``{"phase": "planning" | "step" | "action"}``
      - ``StepType.TOOL_CALL``        -- ``{"name": str, "args": Any,
                                            "result"?: Any, "error"?: str}``
      - ``StepType.AGENT_DELEGATION`` -- ``{"sub_agent_name": str,
                                            "input"?: Any, "output"?: Any}``
      - ``StepType.MESSAGE``          -- ``{"role": "user" | "assistant",
                                            "content": str}``

    ``id`` is a string with a type prefix (e.g. ``"tool_call:abc123"``) and
    is stable across re-polls, so callers can de-duplicate while streaming.
    """

    id: str
    type: StepType
    status: str  # "running" | "completed" | "failed"
    started_at: datetime
    completed_at: datetime | None
    data: dict[str, Any]


@dataclass(frozen=True)
class RunResult:
    """Bundled result of ``client.tasks.run()``.

    Carries the final ``TaskInfo`` snapshot together with the full step
    timeline so callers do not need to re-fetch either after a
    convenience run. The ``status`` and ``output`` properties shortcut
    the most common reads (``result.output`` vs ``result.info.output``).
    """

    info: TaskInfo
    steps: list[Step]

    @property
    def output(self) -> str | None:
        return self.info.output

    @property
    def status(self) -> TaskStatus:
        return self.info.status


# --- Private parsers --------------------------------------------------
# TypeAdapter caches schema per type at module import; cheap to keep at
# module scope. ``validate_python`` handles ISO datetime parsing, enum
# coercion, and Optional handling without bespoke conversion code.

_USER_PRINCIPAL_ADAPTER = TypeAdapter(UserPrincipal)
_TEMPLATE_LIST_ADAPTER = TypeAdapter(list[Template])
_TEMPLATE_DETAIL_ADAPTER = TypeAdapter(TemplateDetail)
_AGENT_LIST_ADAPTER = TypeAdapter(list[AgentSummary])
_AGENT_CREATE_ADAPTER = TypeAdapter(AgentCreateResult)
_ROTATE_KEY_ADAPTER = TypeAdapter(RotateKeyResult)
_CREATE_ADAPTER = TypeAdapter(CreateTaskResult)
_APPEND_ADAPTER = TypeAdapter(AppendResult)
_TASK_INFO_ADAPTER = TypeAdapter(TaskInfo)
_STEP_LIST_ADAPTER = TypeAdapter(list[Step])


def _require_mapping(data: Any, what: str) -> dict[str, Any]:
    """Return ``data`` if it is a dict, else raise ``MalformedResponse``.

    The single-object parsers below trust ``data`` to be a JSON object;
    a backend (or proxy) returning a non-object body would otherwise
    surface as a raw pydantic ``ValidationError`` / ``AttributeError``
    that does not name the real cause. Mirrors the ``isinstance(data,
    list)`` guard the list parsers already apply.
    """
    if not isinstance(data, dict):
        raise MalformedResponse(
            "malformed_response",
            f"expected a JSON object for {what}, got {type(data).__name__}",
            http_status=None,
        )
    return data


def _parse_user_principal(data: Any) -> UserPrincipal:
    return _USER_PRINCIPAL_ADAPTER.validate_python(
        _require_mapping(data, "user principal")
    )


def _parse_template_list(data: Any) -> list[Template]:
    """Parse the raw template array returned by ``GET /v1/templates``.

    Backend returns a bare JSON array of template objects whose primary
    key is ``id``; the SDK surfaces that as ``Template.template_id`` to
    avoid shadowing the ``id`` builtin on public dataclass instances.
    Non-list bodies degrade to an empty list (defense-in-depth against
    upstream proxies returning a non-canonical shape).
    """
    if not isinstance(data, list):
        return []
    normalized = [_template_dict(item) for item in data if isinstance(item, dict)]
    return _TEMPLATE_LIST_ADAPTER.validate_python(normalized)


def _parse_template_detail(data: Any) -> TemplateDetail:
    return _TEMPLATE_DETAIL_ADAPTER.validate_python(
        _template_dict(_require_mapping(data, "template detail"))
    )


def _parse_agent_list(data: Any) -> list[AgentSummary]:
    """Parse the raw agent array returned by ``GET /v1/agents``.

    Same bare-array + ``id`` -> ``agent_id`` rename as
    ``_parse_template_list``.
    """
    if not isinstance(data, list):
        return []
    normalized = [_agent_summary_dict(item) for item in data if isinstance(item, dict)]
    return _AGENT_LIST_ADAPTER.validate_python(normalized)


def _parse_agent_create(data: Any) -> AgentCreateResult:
    """Parse the nested ``{"agent": {...}, "api_key": {...}}`` payload.

    Backend returns the new agent row and (when ``generate_runtime_key``
    is true) the one-time runtime key as two siblings under separate
    keys; SDK flattens them into ``AgentCreateResult`` so callers see one
    record. When ``generate_runtime_key=False`` the ``api_key`` block is
    absent and the runtime fields stay ``None`` -- caller is expected to
    materialize a key via ``rotate_key()`` later.

    Raises ``MalformedResponse`` if the ``agent`` block is missing or
    lacks ``id``/``name`` -- pydantic's raw ``ValidationError`` on
    ``agent_id`` would say "Input should be a valid integer,
    input_value=None" which does not point at the real cause (backend
    response shape violation).
    """
    data = _require_mapping(data, "agent-create")
    agent = data.get("agent")
    if (
        not isinstance(agent, dict)
        or agent.get("id") is None
        or agent.get("name") is None
    ):
        raise MalformedResponse(
            "malformed_response",
            "agent-create response missing required 'agent' block "
            "(expected {'agent': {'id': int, 'name': str, ...}, 'api_key'?: {...}})",
            http_status=None,
        )
    api_key = data.get("api_key")
    if not isinstance(api_key, dict):
        api_key = {}
    flat = {
        "agent_id": agent.get("id"),
        "name": agent.get("name"),
        "runtime_full_key": api_key.get("full_key"),
        "runtime_key_prefix": api_key.get("key_prefix"),
    }
    return _AGENT_CREATE_ADAPTER.validate_python(flat)


def _template_dict(item: dict[str, Any]) -> dict[str, Any]:
    """Surface backend ``id`` as ``template_id`` while passing other keys
    through. Falls back to a pre-existing ``template_id`` so a future
    backend that renames the field at source does not silently degrade
    to ``None``; pydantic drops fields the dataclass does not declare.
    """
    return {**item, "template_id": item.get("id") or item.get("template_id")}


def _agent_summary_dict(item: dict[str, Any]) -> dict[str, Any]:
    """Surface backend ``id`` as ``agent_id`` for the agent list entry,
    with the same ``id``-or-existing-``agent_id`` fallback as
    ``_template_dict``.
    """
    return {**item, "agent_id": item.get("id") or item.get("agent_id")}


def _parse_rotate_key(data: Any) -> RotateKeyResult:
    return _ROTATE_KEY_ADAPTER.validate_python(_require_mapping(data, "key rotation"))


def _parse_create_task(data: Any) -> CreateTaskResult:
    return _CREATE_ADAPTER.validate_python(_require_mapping(data, "task creation"))


def _parse_append(data: Any) -> AppendResult:
    return _APPEND_ADAPTER.validate_python(_require_mapping(data, "task append"))


def _parse_task_info(data: Any) -> TaskInfo:
    return _TASK_INFO_ADAPTER.validate_python(_require_mapping(data, "task info"))


def _parse_steps(data: Any) -> list[Step]:
    """Extract and parse the ``steps`` array from a ``StepsResponse`` body.

    The backend wraps the array in a top-level object with ``task_id`` and
    ``agent_id`` repeated for self-description; the SDK caller invoked
    ``client.tasks.steps(task_id)`` so those wrapper fields are redundant
    and dropped here.

    Defensive against malformed bodies: if ``data`` is not a dict (the
    server or an upstream proxy returned a non-canonical shape) the
    function returns ``[]`` rather than raising ``AttributeError`` from
    inside ``dict.get``. The signature is widened to ``Any`` so callers
    passing ``resp.json()`` (typed ``Any``) do not need to pre-validate.
    """
    if not isinstance(data, dict):
        return []
    return _STEP_LIST_ADAPTER.validate_python(data.get("steps", []))
