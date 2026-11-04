from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import TypeAdapter, ValidationError

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
class AgentSkillsUpdate:
    """``PUT /v1/agents/{agent_id}/skills`` payload.

    ``skills`` is the list the server stored, with a repeated name kept once
    at its first position. ``unresolved_for_caller`` lists the names this
    request added that the key holder cannot see; they are stored anyway.
    """

    agent_id: int
    skills: list[str]
    unresolved_for_caller: list[str]


@dataclass(frozen=True)
class SkillSummary:
    """``GET /v1/skills`` list entry and the body of every skill write.

    ``source`` says where the skill comes from. Today the server sends
    ``"personal"`` (the key holder's own skill), ``"builtin"``, ``"team"``
    or ``"external"``, but it may add values later, so the field is a plain
    ``str`` and any value is passed through unchanged.
    """

    name: str
    description: str
    when_to_use: str
    tags: list[str]
    source: str


@dataclass(frozen=True)
class SkillDetail:
    """``GET /v1/skills/{name}`` payload.

    The fields of ``SkillSummary`` plus ``skill_md`` (the complete
    SKILL.md text) and ``files`` (the relative paths of the skill's files,
    sorted by the server).
    """

    name: str
    description: str
    when_to_use: str
    tags: list[str]
    source: str
    skill_md: str
    files: list[str]


@dataclass(frozen=True)
class CreateTaskResult:
    """``POST /v1/chat/tasks`` 202 payload.

    ``status`` is ``TaskStatus.PENDING`` when the server queues the task
    for its dispatcher, and ``TaskStatus.RUNNING`` when it runs the task
    in-process; observe transitions via
    ``client.tasks.get()`` or ``client.tasks.wait()``.

    ``dispatch_class`` is the class the server gave the start command:
    ``"interactive"`` or ``"batch"`` today, returned as sent so a value
    added later does not break parsing. It is ``None`` when the server
    runs the task in-process rather than through the dispatch queue, or
    when the server predates the field.
    """

    task_id: int
    agent_id: int
    status: TaskStatus
    created_at: datetime
    dispatch_class: str | None = None


@dataclass(frozen=True)
class AppendResult:
    """``POST /v1/chat/tasks/{id}/messages`` 202 payload.

    Also the return type of ``client.tasks.reply()``
    (``POST /v1/chat/tasks/{id}/reply``). The reply response has the
    same fields plus ``command_id``, which this type does not expose; the
    SDK does not carry a separate ``ReplyResult`` for it. For
    ``append()``, ``status`` is ``TaskStatus.PENDING`` when the server
    queues the turn for its dispatcher and ``TaskStatus.RUNNING`` when it
    runs the turn in-process; ``reply()`` documents its own
    status. The timestamp field is ``accepted_at``, not ``created_at``.
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
    is stable across repeated reads of the *same* view -- polling
    ``steps()`` in a loop sees a given step keep the same ``id`` every
    time.

    That stability does **not** carry across views: comparing an ``id``
    read from ``TasksAPI.events()`` against one read from ``steps()`` is
    only meaningful for ``StepType.TOOL_CALL``, ``StepType.AGENT_DELEGATION``,
    and ``StepType.THINKING`` steps whose id embeds the originating
    tool-call or step id. ``StepType.MESSAGE`` steps get a freshly
    generated id on every stream broadcast, different from the id
    ``steps()`` assigns the same persisted row. Planning steps (id
    prefixed ``thinking:plan:`` or ``thinking:planning:``) carry a
    per-connection counter that restarts from zero on every new stream
    connection, so the same id on the stream and on ``steps()`` can name
    two different real steps -- reconcile those by ``started_at`` plus
    content, never by id.
    """

    id: str
    type: StepType
    status: str  # "running" | "completed" | "failed"
    started_at: datetime
    completed_at: datetime | None
    data: dict[str, Any]


class StreamEventType(StrEnum):
    """The 8 event names ``TasksAPI.events()`` can deliver on the wire.

    ``StreamEvent.event`` is deliberately typed as plain ``str``, not
    this enum: a frame whose event name is not one of these 8 is
    dropped per-frame rather than surfacing here (see
    ``TasksAPI.events()``), so widening this enum later to cover a new
    server-side event is additive and does not change how existing
    callers' comparisons against it behave.
    """

    TASK_STATUS = "task.status"
    TASK_COMPLETED = "task.completed"
    TASK_INPUT_REQUIRED = "task.input_required"
    STREAM_ERROR = "stream.error"
    STEP_STARTED = "step.started"
    STEP_COMPLETED = "step.completed"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"


@dataclass(frozen=True)
class StreamEvent:
    """One decoded frame from ``TasksAPI.events()``.

    ``data`` is the frame's JSON payload exactly as the server sent it --
    no key is renamed, added, or removed, including the ``status``
    string on ``task.status`` / ``task.completed`` (kept as ``str``, not
    ``TaskStatus``, so a status value this SDK release does not know
    about still reaches the caller instead of dropping the frame). The
    one exception is a closing frame whose body never arrived: it is
    delivered with an empty ``data`` so the close still reaches the
    caller, which is why closing-frame fields are read with ``.get()``.

    ``step`` is populated only for ``step.started`` / ``step.completed``
    frames and is ``None`` on every other event; it reuses the same
    ``Step`` dataclass ``TasksAPI.steps()`` returns, because the server
    serializes both from the same object. See ``TasksAPI.events()``'s
    "Per-event field reference" section for the full per-event field
    reference, and its docstring generally for the closing-frame
    contract.
    """

    event: str
    data: dict[str, Any]
    step: Step | None


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
_STEP_ADAPTER: TypeAdapter[Step] = TypeAdapter(Step)


@dataclass(frozen=True)
class _SkillDeleted:
    """``DELETE /v1/skills/{name}`` body; only used to check its shape."""

    name: str


@dataclass(frozen=True)
class _AgentSkills:
    """``GET /v1/agents/{agent_id}/skills`` body; only ``skills`` reaches the caller."""

    agent_id: int
    skills: list[str]


_SKILL_SUMMARY_ADAPTER = TypeAdapter(SkillSummary)
_SKILL_LIST_ADAPTER = TypeAdapter(list[SkillSummary])
_SKILL_DETAIL_ADAPTER = TypeAdapter(SkillDetail)
_SKILL_DELETED_ADAPTER = TypeAdapter(_SkillDeleted)
_AGENT_SKILLS_ADAPTER = TypeAdapter(_AgentSkills)
_AGENT_SKILLS_UPDATE_ADAPTER = TypeAdapter(AgentSkillsUpdate)


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


T = TypeVar("T")


def _validate_write(adapter: TypeAdapter[T], data: Any, what: str) -> T:
    """Validate a write endpoint's success body, or raise ``MalformedResponse``.

    The server has already accepted the write when this runs, so a body
    that does not validate must surface as the SDK's documented
    ``MalformedResponse`` rather than a raw pydantic ``ValidationError``.
    Whether resending is safe depends on the endpoint: a retried task
    ``create()`` starts a second task and a retried ``append()``/``reply()``
    can send the message twice, while ``skills.create()``,
    ``skills.upload()`` and ``skills.update()`` are idempotent replays and
    safe to resend.
    ``agents.replace_skills()`` is not: resending can lose the unresolved
    names the first call reported, so call ``agents.get_skills()`` first.
    """
    try:
        return adapter.validate_python(_require_mapping(data, what))
    except ValidationError as exc:
        fields = ", ".join(
            ".".join(str(p) for p in err["loc"]) or "<root>" for err in exc.errors()
        )
        raise MalformedResponse(
            "malformed_response",
            f"{what} response did not match the expected shape ({fields}); "
            "the server accepted the request",
            http_status=None,
        ) from exc


def _validate_read(adapter: TypeAdapter[T], data: Any, what: str) -> T:
    """Validate a read endpoint's body, or raise ``MalformedResponse``.

    A body that does not validate surfaces as the SDK's documented
    ``MalformedResponse`` rather than a raw pydantic ``ValidationError``.
    Nothing was written, so unlike ``_validate_write`` the message does not
    claim that the server accepted anything.
    """
    try:
        return adapter.validate_python(data)
    except ValidationError as exc:
        fields = ", ".join(
            ".".join(str(p) for p in err["loc"]) or "<root>" for err in exc.errors()
        )
        raise MalformedResponse(
            "malformed_response",
            f"{what} response did not match the expected shape ({fields})",
            http_status=None,
        ) from exc


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
    return _validate_write(_CREATE_ADAPTER, data, "task creation")


def _parse_append(data: Any) -> AppendResult:
    return _validate_write(_APPEND_ADAPTER, data, "task append")


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


def _parse_skill_list(data: Any) -> list[SkillSummary]:
    """Parse the bare array returned by ``GET /v1/skills``.

    Unlike the template and agent list parsers, this one is strict: a body
    that is not an array, or any entry that does not match, raises
    ``MalformedResponse``. An empty list is a real answer ("no skills"),
    and a proxy's error page must not be reported as one.
    """
    if not isinstance(data, list):
        raise MalformedResponse(
            "malformed_response",
            f"expected a JSON array for skill list, got {type(data).__name__}",
            http_status=None,
        )
    return _validate_read(_SKILL_LIST_ADAPTER, data, "skill list")


def _parse_skill_detail(data: Any) -> SkillDetail:
    return _validate_read(
        _SKILL_DETAIL_ADAPTER, _require_mapping(data, "skill detail"), "skill detail"
    )


def _parse_skill_write(data: Any, what: str) -> SkillSummary:
    return _validate_write(_SKILL_SUMMARY_ADAPTER, data, what)


def _parse_skill_delete(data: Any) -> None:
    """Check that a delete answered with ``{"name": str}``.

    The body carries nothing the caller needs, but a 200 that is not the
    server's delete confirmation (for example a proxy's HTML page) must not
    be reported as a completed delete.
    """
    _validate_write(_SKILL_DELETED_ADAPTER, data, "skill deletion")


def _parse_agent_skills(data: Any) -> list[str]:
    """Parse ``GET /v1/agents/{agent_id}/skills`` strictly.

    The result is usually edited and written back, so a body of the wrong
    shape must raise rather than read as an empty list.
    """
    return _validate_read(
        _AGENT_SKILLS_ADAPTER,
        _require_mapping(data, "agent skill list"),
        "agent skill list",
    ).skills


def _parse_agent_skills_update(data: Any) -> AgentSkillsUpdate:
    return _validate_write(_AGENT_SKILLS_UPDATE_ADAPTER, data, "agent skill update")
