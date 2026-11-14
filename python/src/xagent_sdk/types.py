import copy
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Generic, overload

from pydantic import BaseModel, TypeAdapter, ValidationError
from typing_extensions import TypeVar

from xagent_sdk._structured import ModelT as _ModelT
from xagent_sdk._structured import parse_with_model
from xagent_sdk.errors import MalformedResponse, StructuredOutputError, _run_mismatch


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
class RuntimeKeyInfo:
    """One runtime key of an agent, as listed by ``agents.list_keys()``.

    Never carries the secret. ``key_prefix`` is the public middle segment of
    the full key (``xag_<key_prefix>_<secret>``), so a key you hold can be
    matched to its entry. ``status`` is ``"active"``, ``"paused"`` or
    ``"revoked"``; only an active key is accepted by the server.
    """

    key_id: int
    label: str | None
    key_prefix: str
    masked_key: str
    status: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True)
class RevokeKeyResult:
    """``DELETE /v1/agents/{agent_id}/api-keys/{key_id}`` payload.

    ``revoked`` is ``True`` when this call revoked the key and ``False`` when
    it had already been revoked; ``revoked_at`` is when it was revoked either
    way. ``key_prefix`` identifies the secret that was revoked.
    """

    key_id: int
    key_prefix: str
    revoked: bool
    revoked_at: datetime


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
class McpServerAuthorization:
    """The key holder's own OAuth authorization of an MCP connector.

    ``kind`` is ``"mcp_oauth"`` for a connector with its own OAuth settings
    or ``"builtin_oauth"`` for a platform app connection. ``status`` is
    ``"authorized"``, ``"not_authorized"``, ``"needs_reconnect"`` or
    ``"configuration_error"`` today. The server may add values to both, so
    they are plain ``str`` and passed through unchanged. ``message`` says
    what the holder can do next, or is None when nothing is needed;
    authorization itself can only be completed in the xAgent web app.
    """

    kind: str
    status: str
    message: str | None


@dataclass(frozen=True)
class McpServer:
    """One MCP connector the key holder can see (``/v1/mcp-servers``).

    The fields mirror the server's response one to one; every field is
    always present in a response, so a missing one is reported as
    ``MalformedResponse``.

    Fields with a fixed set of values -- ``transport``, ``access``,
    ``kind`` -- are plain ``str``; the server may add values, which are
    passed through unchanged. ``access`` is ``"own"`` when the holder has a
    link to the connector (switched on or off) and ``"team"`` when it is
    visible only because the holder's team shares it; for a team connector
    ``enabled`` and ``user_env`` are None. ``kind`` is ``"custom"`` for a
    connector a user created from a URL and ``"catalog"`` for a platform
    app connection. ``can_edit`` and ``can_delete`` are hints from the
    server, not a guarantee that a change will be accepted.

    These secret values are masked: header values, the values of ``env``
    and ``user_env``, and the ``bearer_token``, ``api_key_value``,
    ``client_secret`` and ``access_token`` entries of ``auth`` read
    ``"********"`` when they are set. Everything else, including other
    ``auth`` entries and a secret written into ``url`` or
    ``runtime_bindings``, is returned as stored.

    ``authorization`` is None unless the connector uses OAuth (see
    ``McpServerAuthorization``). ``created_at`` and ``updated_at`` are the
    server's strings, passed through unchanged.
    """

    id: int
    name: str
    description: str | None
    transport: str
    url: str | None
    headers: dict[str, Any] | None
    auth: dict[str, Any] | None
    env: dict[str, Any] | None
    user_env: dict[str, Any] | None
    runtime_input_schema: dict[str, Any] | None
    runtime_bindings: Any
    signed_uploads: Any
    signed_uploads_error: str | None
    allow_delegated_authorization: bool
    access: str
    kind: str
    enabled: bool | None
    can_edit: bool
    can_delete: bool
    app_id: str | None
    provider: str | None
    connected_account: str | None
    authorization: McpServerAuthorization | None
    created_at: str | None
    updated_at: str | None


@dataclass(frozen=True)
class McpTool:
    """One tool an MCP connector offers.

    ``mcp_name`` is the tool's name on the MCP server; it is the stable
    identifier. ``name`` is the display name the web app shows, which is not
    stable -- do not use it to refer to the tool.
    """

    name: str
    mcp_name: str
    description: str


@dataclass(frozen=True)
class McpToolLoadFailure:
    """One failed step while the server loaded a connector's tools.

    ``phase`` names the step and ``attempts`` is how many times the server
    tried it. ``phase`` is a plain ``str`` because the server may add values;
    it is passed through unchanged.
    """

    phase: str
    attempts: int


@dataclass(frozen=True)
class McpServerTools:
    """The tools of one MCP connector (``GET /v1/mcp-servers/{id}/tools``).

    ``id`` is the connector id. ``tools`` is never empty in a success
    response: the server answers 502 (``ConnectorToolsUnavailable``) when it
    loaded no tools. A non-empty ``failures`` means some part of the loading
    failed even though tools were returned, so the list may be incomplete.
    """

    id: int
    tools: list[McpTool]
    failures: list[McpToolLoadFailure]


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

    ``run_id`` identifies the run this request started. Pass it to
    ``client.tasks.wait(..., expected_run_id=...)`` to make sure the
    snapshot you get back belongs to this run. It is ``None`` when the
    server predates the field.
    """

    task_id: int
    agent_id: int
    status: TaskStatus
    created_at: datetime
    dispatch_class: str | None = None
    run_id: str | None = None


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

    ``run_id`` identifies the run the turn belongs to: the new run an
    ``append()`` started, or, for ``reply()``, the run the task already
    had, which the answer resumes. Pass it to
    ``client.tasks.wait(..., expected_run_id=...)`` to make sure the
    snapshot you get back belongs to that run. For ``reply()`` that only
    rules out another run: a new question the same run asks after your
    answer has the same ``run_id`` (see ``reply()`` on comparing the
    question). It is ``None`` when the server predates the field.
    """

    task_id: int
    agent_id: int
    status: TaskStatus
    accepted_at: datetime
    run_id: str | None = None


@dataclass(frozen=True)
class CancelResult:
    """``POST /v1/chat/tasks/{id}/cancel`` payload.

    ``cancelled`` is ``True`` when the server answered that the task is
    cancelled (HTTP 200), whether this call did it or an earlier one already
    had. It is ``False`` when the cancel was recorded but not applied within
    the server's wait (HTTP 202): ``status`` is still the task's current one,
    ``cancelled_run_id`` and ``error_code`` are ``None``, and calling
    ``cancel()`` again reaches the same cancel. A paused task whose
    automatic resume gave up meanwhile reads ``FAILED`` here, and the cancel
    still applies to it.

    The other fields describe the task as ``get()`` reads it now. Once
    cancelled it reads ``TaskStatus.FAILED`` with
    ``error_code == "task_cancelled"``, unless a later turn followed: while
    a turn appended after the cancelled run is still queued to start it
    reads ``PENDING`` with ``error_code`` ``None``, as ``get()`` does
    (``run_id`` is still the cancelled run), and once one has started
    ``status``, ``run_id``, ``state_version`` and ``error_code`` describe
    that turn.

    ``cancelled_run_id`` is the run that was cancelled, so it differs from
    ``run_id`` once a later turn has started, and when a turn was withdrawn
    while it was still queued to start: a new task then has no ``run_id``
    at all, and a task you appended to keeps showing the turn before it --
    its ``status`` and ``error_code`` are that turn's, not the withdrawn
    one's.
    """

    task_id: int
    status: TaskStatus
    state_version: int
    run_id: str | None = None
    cancelled_run_id: str | None = None
    error_code: str | None = None
    cancelled: bool = False


class StructuredOutputStatus:
    """The values of ``TaskInfo.structured_output_status`` known to this SDK.

    The field itself is a plain ``str``, so a value a later server adds still
    reaches you unchanged.
    """

    READY = "ready"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class StructuredOutputReasonCode:
    """The values of ``StructuredOutputReason.code`` and
    ``StructuredOutputError.code`` known to this SDK.

    The server reports the first eleven in ``TaskInfo.structured_output_reason``:
    ``agent_partial``, ``agent_blocked``, ``information_missing``,
    ``value_not_expressible`` and ``not_produced`` with status ``incomplete``;
    the rest with status ``failed``. Fields of ``StructuredOutputReason`` and
    ``StructuredOutputError`` are plain ``str``, so a code a later server adds
    still reaches you unchanged.

    ``RUN_MISMATCH``, ``CLIENT_VALIDATION_FAILED`` and ``NO_STRUCTURED_OUTPUT``
    are raised by this SDK only; the server never sends them.
    ``CLIENT_VALIDATION_FAILED`` means only that a ready result does not
    validate with the model passed to ``parsed(model)``; a model that cannot
    be converted raises ``UnsupportedModel`` instead.
    """

    AGENT_PARTIAL = "agent_partial"
    AGENT_BLOCKED = "agent_blocked"
    INFORMATION_MISSING = "information_missing"
    VALUE_NOT_EXPRESSIBLE = "value_not_expressible"
    NOT_PRODUCED = "not_produced"
    SCHEMA_MISMATCH = "schema_mismatch"
    RESULT_TOO_LARGE = "result_too_large"
    MODEL_ERROR = "model_error"
    INVALID_STORED_FORMAT = "invalid_stored_format"
    EXTRACTION_TIMEOUT = "extraction_timeout"
    INTERNAL_ERROR = "internal_error"
    RUN_MISMATCH = "run_mismatch"
    CLIENT_VALIDATION_FAILED = "client_validation_failed"
    NO_STRUCTURED_OUTPUT = "no_structured_output"


# The codes in ``StructuredOutputReasonCode`` that only this SDK raises.
_SDK_ONLY_REASON_CODES = frozenset(
    {
        StructuredOutputReasonCode.RUN_MISMATCH,
        StructuredOutputReasonCode.CLIENT_VALIDATION_FAILED,
        StructuredOutputReasonCode.NO_STRUCTURED_OUTPUT,
    }
)


@dataclass(frozen=True)
class StructuredOutputReason:
    """Why a task has no structured result, or only a partial one:
    ``TaskInfo.structured_output_reason``.

    ``code`` is one of the server codes in ``StructuredOutputReasonCode`` (or a
    code a later server adds) and ``message`` the server's sentence for it.
    ``missing_fields`` lists, as JSON Pointers, up to 20 positions the answer
    did not state; ``unexpressible_fields`` up to 20 positions it stated in a
    form the schema cannot hold. Both are empty when the server sent none. They
    come with ``information_missing`` and ``value_not_expressible``, and, when
    the task's ``response_format`` sets ``accept_partial``, also with
    ``agent_partial`` and ``agent_blocked`` on a task whose
    ``structured_output`` holds values.
    """

    code: str
    message: str
    missing_fields: list[str] = field(default_factory=list)
    unexpressible_fields: list[str] = field(default_factory=list)


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

    ``run_id`` identifies the run this snapshot shows; ``None`` when the
    task has none or the server predates the field. Compare it with the
    ``run_id`` of the ``create()`` or ``append()`` that started the run
    you are waiting for; ``client.tasks.wait(..., expected_run_id=...)``
    does this for you.

    ``structured_output``, ``structured_output_status`` and
    ``structured_output_reason`` report the structured result of the run this
    snapshot shows, when the task has a ``response_format``. They are set only
    on a ``COMPLETED`` task; otherwise, and on a server that predates them,
    all three are ``None``. ``structured_output_status`` is ``"ready"``,
    ``"incomplete"`` or ``"failed"`` (see ``StructuredOutputStatus``; kept as a
    plain ``str``). When it is ``"ready"``, ``structured_output`` holds the
    result and the reason is ``None``. Otherwise ``structured_output_reason``
    says why and ``structured_output`` is ``None``, except that when the task's
    ``response_format`` sets ``accept_partial`` and the status is
    ``"incomplete"``, it holds the values the server could extract, with
    ``None`` where a value is missing or cannot be written; it is still
    ``None`` when nothing could be kept, so check it before reading it.
    ``parsed()`` reads these fields for you and raises for such a task, with
    the values in ``StructuredOutputError.partial_output``.

    ``completion_outcome`` is how the agent rated the run: ``"completed"``,
    ``"partial"`` or ``"blocked"`` (kept as a plain ``str``). It is ``None``
    when the task's response format does not set accept_partial (the server
    then omits the key), or when the run is not completed or recorded no
    rating. A missing key and a null both read as ``None``. Whether the
    structured result is complete is told by ``structured_output_status``, not
    by this field.

    ``error_code`` is a machine-readable reason for a ``FAILED`` status when
    the server defines one: ``"task_cancelled"`` for a task stopped with
    ``client.tasks.cancel()``. ``None`` for every other failure and status,
    including while a turn appended after a cancelled run is queued to
    start or running,
    and on a server that predates the field. Kept as a plain ``str``. The
    closing ``task.completed`` frame of ``events()`` carries the same value
    under ``data["error_code"]``.
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
    run_id: str | None = None
    structured_output: dict[str, Any] | None = None
    structured_output_status: str | None = None
    structured_output_reason: StructuredOutputReason | None = None
    completion_outcome: str | None = None
    error_code: str | None = None

    @overload
    def parsed(
        self, model: None = None, *, expected_run_id: str | None = None
    ) -> dict[str, Any]: ...

    @overload
    def parsed(
        self, model: type[_ModelT], *, expected_run_id: str | None = None
    ) -> _ModelT: ...

    def parsed(
        self,
        model: type[BaseModel] | None = None,
        *,
        expected_run_id: str | None = None,
    ) -> dict[str, Any] | BaseModel:
        """The structured result of this snapshot.

        With ``expected_run_id``, the snapshot must show that run first;
        another run or no run (``run_id`` is ``None``) raises
        ``RunMismatch`` (code ``run_mismatch``), a subclass of
        ``StructuredOutputError``. Then, when ``structured_output_status``
        is ``"ready"``, returns a deep copy of ``structured_output``, or,
        with ``model``, that copy validated as ``model``; the snapshot
        itself is not changed.

        A field of ``model`` that has a default value reads as null in
        ``structured_output`` when the answer did not state it. With
        ``model``, those nulls are removed before validation so the defaults
        apply; an ``Optional`` field whose default is not ``None`` stays
        ``None``. Without ``model`` the dict keeps the nulls.

        Raises:
            RunMismatch: ``expected_run_id`` was given and the snapshot
                shows another run or no run (code ``run_mismatch``).
            StructuredOutputError: the status is not ``"ready"`` (``code``,
                ``message`` and the two field lists come from
                ``structured_output_reason``, and ``status`` is the
                status; ``partial_output`` is a copy of
                ``structured_output`` when the status is ``"incomplete"`` and
                it holds values); or the snapshot has no structured
                result at all, because the task is not ``COMPLETED``, has no
                ``response_format`` or the server predates the fields
                (``no_structured_output``, ``status`` ``None``); or
                ``model`` does not validate the ready result, for example
                because of a ``format`` or length constraint the server does
                not check (``client_validation_failed``, status ``"ready"``,
                the ``ValidationError`` as ``__cause__``).
            UnsupportedModel: the status is ``"ready"`` and ``model`` cannot
                be converted into the supported schema subset, as when it is
                passed as ``response_format``; a subclass of
                ``UnsupportedSchema``, not of ``StructuredOutputError``.
            MalformedResponse: the fields break the server's contract: a
                ``"ready"`` status without an object, or another status
                without a reason.

        An error Pydantic raises while generating the model's JSON schema
        (for example ``PydanticInvalidForJsonSchema`` for a ``Callable``
        field) propagates unchanged.
        """
        if expected_run_id is not None and self.run_id != expected_run_id:
            raise _run_mismatch(self.task_id, expected_run_id, self.run_id)
        status = self.structured_output_status
        if status is None:
            raise StructuredOutputError(
                StructuredOutputReasonCode.NO_STRUCTURED_OUTPUT,
                "The task snapshot carries no structured result: the task is "
                "not completed, has no response_format, or the server does not "
                "report structured results.",
            )
        if status == StructuredOutputStatus.READY:
            if type(self.structured_output) is not dict:
                raise _malformed_structured_output(
                    "structured_output_status is 'ready' but structured_output "
                    "is not an object"
                )
            output = copy.deepcopy(self.structured_output)
            if model is None:
                return output
            return parse_with_model(output, model)
        reason = self.structured_output_reason
        if reason is None:
            raise _malformed_structured_output(
                f"structured_output_status is {status!r} but "
                "structured_output_reason is missing"
            )
        raise StructuredOutputError(
            reason.code,
            reason.message,
            status=status,
            missing_fields=reason.missing_fields,
            unexpressible_fields=reason.unexpressible_fields,
            partial_output=(
                copy.deepcopy(self.structured_output)
                if status == StructuredOutputStatus.INCOMPLETE
                and type(self.structured_output) is dict
                else None
            ),
        )


def _malformed_structured_output(detail: str) -> MalformedResponse:
    return MalformedResponse("malformed_response", detail, http_status=None)


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


# What ``RunResult.parsed()`` returns without an argument: the model ``run()``
# was given, or the result as a dict.
_ParsedT = TypeVar("_ParsedT", covariant=True, default=dict[str, Any])


@dataclass(frozen=True, init=False)
class RunResult(Generic[_ParsedT]):
    """Bundled result of ``client.tasks.run()``.

    Carries the final ``TaskInfo`` snapshot together with the full step
    timeline so callers do not need to re-fetch either after a
    convenience run. The ``status`` and ``output`` properties shortcut
    the most common reads (``result.output`` vs ``result.info.output``).

    ``run_id`` is the run ``run()`` started (``CreateTaskResult.run_id``);
    ``None`` when the server predates the field or the result was built
    without one. ``run()`` has already checked that ``info`` belongs to
    that run. The ``structured_output*`` properties and ``completion_outcome``
    read the fields of the same name on ``info``, and ``parsed()`` is
    ``info.parsed()`` checked against ``run_id``.

    ``model`` is the Pydantic model class ``run()`` was given as
    ``response_format``, and ``None`` for a ``dict`` or no format.
    ``parsed()`` without an argument validates the result as ``model`` when
    there is one, and ``parsed(other)`` as ``other``. The type parameter is
    what ``parsed()`` returns without an argument: ``run(response_format=
    Invoice)`` returns a ``RunResult[Invoice]``, and a result without a
    model is a ``RunResult[dict[str, Any]]``, which a bare ``RunResult``
    annotation also means. Build one with ``model=`` as a keyword.
    """

    info: TaskInfo
    steps: list[Step]
    run_id: str | None = None
    model: type[BaseModel] | None = None

    @overload
    def __init__(
        self: "RunResult[dict[str, Any]]",
        info: TaskInfo,
        steps: list[Step],
        run_id: str | None = None,
        model: None = None,
    ) -> None: ...

    @overload
    def __init__(
        self: "RunResult[_ModelT]",
        info: TaskInfo,
        steps: list[Step],
        run_id: str | None = None,
        *,
        model: type[_ModelT],
    ) -> None: ...

    def __init__(
        self,
        info: TaskInfo,
        steps: list[Step],
        run_id: str | None = None,
        model: type[BaseModel] | None = None,
    ) -> None:
        object.__setattr__(self, "info", info)
        object.__setattr__(self, "steps", steps)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "model", model)

    @property
    def output(self) -> str | None:
        return self.info.output

    @property
    def status(self) -> TaskStatus:
        return self.info.status

    @property
    def structured_output(self) -> dict[str, Any] | None:
        return self.info.structured_output

    @property
    def structured_output_status(self) -> str | None:
        return self.info.structured_output_status

    @property
    def structured_output_reason(self) -> StructuredOutputReason | None:
        return self.info.structured_output_reason

    @property
    def completion_outcome(self) -> str | None:
        return self.info.completion_outcome

    @overload
    def parsed(self, model: None = None) -> _ParsedT: ...

    @overload
    def parsed(self, model: type[_ModelT]) -> _ModelT: ...

    def parsed(self, model: type[BaseModel] | None = None) -> Any:
        """``self.info.parsed(model, expected_run_id=self.run_id)``, with
        ``self.model`` in place of a ``model`` that is ``None``: the
        structured result, after checking that ``info`` shows the run
        ``run()`` started, validated as ``model`` when given, else as
        ``self.model`` when set, else returned as a dict."""
        return self.info.parsed(
            model if model is not None else self.model, expected_run_id=self.run_id
        )


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
_RUNTIME_KEY_LIST_ADAPTER = TypeAdapter(list[RuntimeKeyInfo])
_REVOKE_KEY_ADAPTER = TypeAdapter(RevokeKeyResult)
_CREATE_ADAPTER = TypeAdapter(CreateTaskResult)
_APPEND_ADAPTER = TypeAdapter(AppendResult)
_TASK_INFO_ADAPTER = TypeAdapter(TaskInfo)
_CANCEL_ADAPTER = TypeAdapter(CancelResult)
_STEP_LIST_ADAPTER = TypeAdapter(list[Step])
_STEP_ADAPTER: TypeAdapter[Step] = TypeAdapter(Step)


@dataclass(frozen=True)
class _SkillDeleted:
    """``DELETE /v1/skills/{name}`` body; only used to check its shape."""

    name: str


@dataclass(frozen=True)
class _McpServerDeleted:
    """``DELETE /v1/mcp-servers/{id}`` body; only used to check its shape."""

    id: int


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
_MCP_SERVER_ADAPTER = TypeAdapter(McpServer)
_MCP_SERVER_LIST_ADAPTER = TypeAdapter(list[McpServer])
_MCP_SERVER_TOOLS_ADAPTER = TypeAdapter(McpServerTools)
_MCP_SERVER_DELETED_ADAPTER = TypeAdapter(_McpServerDeleted)


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
    safe to resend, and so are ``agents.revoke_key()``, which reports an
    already revoked key with ``revoked=False``, and ``mcp_servers.update()``
    and ``mcp_servers.set_enabled()``; ``mcp_servers.create()`` is not (a
    repeat gets ``ConnectorNameConflict``).
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


def _key_id_dict(item: Any) -> Any:
    """Rename the server's ``id`` to the SDK's ``key_id``.

    Anything that is not an object, or has no ``id``, is passed through
    unchanged so validation reports it instead of this helper hiding it.
    """
    if not isinstance(item, dict) or "id" not in item:
        return item
    renamed = dict(item)
    renamed["key_id"] = renamed.pop("id")
    return renamed


def _parse_runtime_key_list(data: Any) -> list[RuntimeKeyInfo]:
    """Parse ``GET /v1/agents/{agent_id}/api-keys`` strictly.

    The result decides which keys a caller revokes or keeps, so a body of the
    wrong shape must raise rather than read as "this agent has no keys".
    """
    what = "runtime key list"
    if type(data) is not list:
        raise MalformedResponse(
            "malformed_response",
            f"{what} response did not match the expected shape (<root>)",
            http_status=None,
        )
    return _validate_read(
        _RUNTIME_KEY_LIST_ADAPTER, [_key_id_dict(item) for item in data], what
    )


def _parse_revoke_key(data: Any) -> RevokeKeyResult:
    return _validate_write(
        _REVOKE_KEY_ADAPTER, _key_id_dict(data), "runtime key revocation"
    )


def _parse_create_task(data: Any) -> CreateTaskResult:
    return _validate_write(_CREATE_ADAPTER, data, "task creation")


def _parse_append(data: Any) -> AppendResult:
    return _validate_write(_APPEND_ADAPTER, data, "task append")


def _parse_cancel(data: Any, *, cancelled: bool) -> CancelResult:
    """Parse a cancel body; ``cancelled`` comes from the HTTP status, which
    is where the server says whether the cancel was applied."""
    body = dict(_require_mapping(data, "task cancel"))
    body.pop("cancelled", None)
    result = _validate_write(_CANCEL_ADAPTER, body, "task cancel")
    return replace(result, cancelled=cancelled)


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
    passing a decoded JSON body (typed ``Any``) do not need to pre-validate.
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


def _parse_mcp_server_list(data: Any) -> list[McpServer]:
    """Parse the bare array returned by ``GET /v1/mcp-servers`` strictly.

    A body that is not an array, or any entry that does not match, raises
    ``MalformedResponse``: an empty list is a real answer ("no connectors"),
    and a proxy's error page must not be reported as one.
    """
    if not isinstance(data, list):
        raise MalformedResponse(
            "malformed_response",
            f"expected a JSON array for connector list, got {type(data).__name__}",
            http_status=None,
        )
    return _validate_read(_MCP_SERVER_LIST_ADAPTER, data, "connector list")


def _parse_mcp_server_read(data: Any, what: str) -> McpServer:
    return _validate_read(_MCP_SERVER_ADAPTER, _require_mapping(data, what), what)


def _parse_mcp_server_write(data: Any, what: str) -> McpServer:
    return _validate_write(_MCP_SERVER_ADAPTER, data, what)


def _parse_mcp_server_delete(data: Any) -> None:
    """Check that a delete answered with ``{"id": int}``.

    The body carries nothing the caller needs, but a 200 that is not the
    server's delete confirmation (for example a proxy's HTML page) must not
    be reported as a completed delete.
    """
    _validate_write(_MCP_SERVER_DELETED_ADAPTER, data, "connector deletion")


def _parse_mcp_server_tools(data: Any) -> McpServerTools:
    what = "connector tools"
    return _validate_read(_MCP_SERVER_TOOLS_ADAPTER, _require_mapping(data, what), what)


def _parse_features(data: Any) -> list[str]:
    """Parse ``GET /v1/features``: ``{"features": [str, ...]}``.

    Any other shape raises ``MalformedResponse``, so a proxy's page is never
    read as "no features".
    """
    features = _require_mapping(data, "features").get("features")
    if type(features) is not list or not all(type(f) is str for f in features):
        raise MalformedResponse(
            "malformed_response",
            "features response did not match the expected shape (features)",
            http_status=None,
        )
    return features
