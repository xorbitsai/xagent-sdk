import re
from typing import Any

import httpx

_MAX_FALLBACK_ERROR_MESSAGE_LENGTH = 1000


class XAgentError(Exception):
    """Base class for all SDK exceptions.

    Attributes:
        code: stable machine-readable identifier (e.g. ``invalid_api_key``).
        message: human-readable description; may change between server versions.
        http_status: HTTP status code if the error came from an HTTP response;
            None for transport-level failures and client-side timeouts.
        details: the ``details`` object of the server's error response, as
            sent, when the server attached one; otherwise None. Its keys
            depend on the error code and may grow between server versions.
            It can be ``{}``, which is not the same as None. Errors the SDK
            raises itself, and responses without the V1 error envelope, have
            None.
        retry_after: the whole number of seconds the server's ``Retry-After``
            response header asks the caller to wait, when the error came
            with the V1 error envelope and a header of that form; otherwise
            None. ``0`` means the request can be retried at once, which is
            different from None (no advice), so test it with
            ``is not None``. Dates and other header forms are not read.
    """

    code: str
    message: str
    http_status: int | None
    details: dict[str, Any] | None
    retry_after: int | None

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int | None = None,
        *,
        details: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details
        self.retry_after = retry_after


# Server-mapped errors (V1 envelope stable codes).
class InvalidAPIKey(XAgentError):
    """HTTP 401, code ``invalid_api_key``."""


class AgentNotFound(XAgentError):
    """HTTP 404, code ``agent_not_found``."""


class TaskNotFound(XAgentError):
    """HTTP 404, code ``task_not_found``."""


class TaskBusy(XAgentError):
    """HTTP 409, code ``task_busy``. Retryable (caller decides when).

    Raised when the task is currently ``RUNNING`` or has an earlier turn
    still queued, and cannot accept a new turn or a reply yet, or when a
    concurrent ``reply()`` already claimed it. ``TemporarilyUnavailable``
    is the other retryable code in this hierarchy.

    When ``reply()`` raises it because the server did not take the answer,
    ``details["accepted"]`` is ``False``: nothing was written and the reply
    can be sent again. ``details`` is None when the task is simply busy.
    """


class RateLimited(XAgentError):
    """HTTP 429, code ``rate_limited``.

    ``UserClient.mcp_servers.list_tools()`` raises it when your account
    already has two tool listings in progress on that server process
    (including ones whose request ended early), or when no free slot for
    another listing turns up in time (the slots are shared by all key
    holders). ``retry_after`` is then the number of seconds the server
    suggests waiting before trying again.
    """


class InternalError(XAgentError):
    """HTTP 500, code ``internal_error``.

    Also used as the fallback for unknown codes or malformed error bodies.
    """


class InvalidInput(XAgentError):
    """HTTP 400, 413 or 422, code ``invalid_input``.

    Current ``/v1/*`` endpoints return this through the V1 envelope.
    The skill endpoints (``UserClient.skills``) use 400 for invalid
    SKILL.md content and 413 for a request over a size limit; request
    validation uses 422. ``http_status`` keeps the status the server sent.
    The SDK also accepts FastAPI's legacy ``{"detail": [...]}`` validation
    shape as a compatibility fallback.
    """


class TemplateNotFound(XAgentError):
    """HTTP 404, code ``template_not_found``.

    Raised when ``UserClient.templates.get(template_id)`` or
    ``UserClient.agents.create_from_template(template_id, ...)`` is
    given an unknown ``template_id``. Distinct from ``AgentNotFound``
    because the SaaS UI path treats template-picker mismatch differently
    from agent-lookup mismatch.
    """


class APIKeyNotFound(XAgentError):
    """HTTP 404, code ``api_key_not_found``.

    Raised by ``agents.revoke_key()``: the agent is one the caller can
    manage, but it has no runtime key with this id. A key that belongs to
    another agent is reported the same way as an id that does not exist.
    """


class SkillNotFound(XAgentError):
    """HTTP 404, code ``skill_not_found``.

    Raised by ``UserClient.skills.get/update/delete``: no skill the key holder can
    see has this name (``get``), or the name is not one of the holder's own
    personal skills (``update`` / ``delete``).
    """


class SkillAlreadyExists(XAgentError):
    """HTTP 409, code ``skill_already_exists``.

    Raised by ``UserClient.skills.create`` / ``upload``: the holder already has
    a personal skill with this name and this request is not a repeat of the
    write that created it (a repeat through the other creation method also
    counts as a conflict).
    """


class ConnectorNotFound(XAgentError):
    """HTTP 404, code ``connector_not_found``.

    Raised by ``UserClient.mcp_servers`` methods that take a ``server_id``:
    the key holder cannot see a connector with this id. A connector that
    does not exist and one the holder cannot see get the same answer.
    """


class TransportNotSupported(XAgentError):
    """HTTP 400, code ``transport_not_supported``.

    Raised by ``UserClient.mcp_servers.list_tools()`` when the connector
    uses a transport the API cannot list tools for: only
    ``streamable_http`` and ``sse`` are supported. Also raised by
    ``create()`` and ``update()`` when the ``transport`` they are given is
    not one of those two, and by ``update()`` when the connector's stored
    transport is not. ``details`` is None.
    """


class ConnectorNameConflict(XAgentError):
    """HTTP 409, code ``connector_name_conflict``.

    Raised by ``UserClient.mcp_servers.create()`` and ``update()`` when the
    connector name is already in use or reserved. Connector names are unique
    across all users and are compared case-insensitively, treating spaces,
    hyphens and underscores as the same character.

    ``details["connector_id"]`` is present only when the name is taken by
    one of the key holder's own connectors; it is absent when another user
    has the name. After a ``create()`` that timed out, it can be the
    connector the first attempt created, but it can also be one that
    existed before, so read it with ``get()`` first. After an ``update()``
    that renames a connector it is the key holder's *other* connector with
    that name: it does not mean the rename went through.
    """


class ConnectorNotEditable(XAgentError):
    """HTTP 409, code ``connector_not_editable``.

    Raised by ``UserClient.mcp_servers.update()`` and ``delete()`` when the
    connector cannot be changed through the API. ``details["reason"]`` says
    why: ``catalog`` (an app connection from the catalog, which can only be
    changed in the web app), ``managed_elsewhere`` (another account manages
    its OAuth connection), ``authorized_by_others`` (other accounts hold
    authorizations that changing ``url``, ``auth.issuer`` or
    ``auth.resource_metadata_url`` would keep; make that change in the web
    app) or
    ``unknown``. Treat any other string as an unknown reason.
    """


class ConnectorForbidden(XAgentError):
    """HTTP 403, code ``connector_forbidden``.

    Raised by ``UserClient.mcp_servers.update()``, ``delete()`` and
    ``set_enabled()`` when the key holder may not make the change, and by
    ``create()`` when the server refuses the holder a new connector.
    ``details["reason"]`` says why: ``team_shared`` (the connector reaches
    the holder only through their team, so it is read-only) or
    ``not_permitted`` (the holder lacks the right to change it, for example
    deleting a connector owned by the team without being a team
    administrator). Treat any other string as ``not_permitted``.
    """


class ConnectorUnavailable(XAgentError):
    """HTTP 409, code ``connector_unavailable``.

    Raised by ``UserClient.mcp_servers.list_tools()`` when the connector
    exists but its tools cannot be listed right now.
    ``details["reason"]`` says why: ``team_shared``, ``disabled``,
    ``unsupported``, ``client_rejected``, ``unknown``, or a reason string
    of the connector session itself (lowercase letters and underscores).
    Treat any other string as an unknown reason.
    """


class ConnectorToolsUnavailable(XAgentError):
    """HTTP 502, code ``connector_tools_unavailable``.

    Raised by ``UserClient.mcp_servers.list_tools()`` when the connector
    was started but did not come up -- including when its URL cannot be
    reached -- or came up with no tools (an empty tool list is never
    returned as a success). ``details["reason"]`` is a short reason string
    and ``details["failures"]`` is a list, possibly empty, of
    ``{"phase": str, "attempts": int}`` objects describing what failed.
    """


class McpOAuthAuthorizationFailed(XAgentError):
    """HTTP 409, code ``mcp_oauth_authorization_failed``.

    Raised by ``UserClient.mcp_servers.list_tools()`` when the connector's
    OAuth authorization is missing or no longer works. The SDK cannot
    complete it: authorization can only be done in the web app.
    ``details["reason"]`` says what is wrong; the server uses
    ``authorization_required`` when it gives no other reason.
    """


class InteractionResponseRequired(XAgentError):
    """HTTP 409, code ``interaction_response_required``.

    Raised by ``append()`` when the task is currently ``WAITING_FOR_USER``:
    the task has a pending question and cannot accept a new turn through
    the append channel. Call ``reply()`` instead -- inspect
    ``TaskInfo.pending_interaction`` first to see the question.
    """


class NoPendingInteraction(XAgentError):
    """HTTP 409, code ``no_pending_interaction``.

    Raised by ``reply()`` when the task is not currently
    ``WAITING_FOR_USER``: it is ``PENDING`` (has not started running
    yet), ``PAUSED``, or already terminal (``COMPLETED``/``FAILED``).
    A ``RUNNING`` task raises ``TaskBusy`` instead -- being busy is a
    different condition from having no pending question. Do not retry
    this call as-is. A ``PAUSED``, ``COMPLETED``, or ``FAILED`` task can
    take its next turn via ``append()``; a ``PENDING`` task cannot --
    there is nothing to append to until it starts running.
    """


class InteractionNotResumable(XAgentError):
    """HTTP 409, code ``interaction_not_resumable``.

    Raised by ``reply()`` when the task is ``WAITING_FOR_USER`` but the
    execution state behind it could not be restored well enough to
    deliver the answer -- confirmed unusable, not a transient hiccup.
    The task is left exactly as it was (still ``WAITING_FOR_USER``, same
    ``run_id``, no partial write): do not retry, start a new task
    instead.
    """


class TemporarilyUnavailable(XAgentError):
    """HTTP 503, code ``temporarily_unavailable``.

    Raised by ``reply()`` when the task's execution state could not be
    read because of a transient failure -- unlike
    ``InteractionNotResumable``, the server cannot yet tell whether the
    state is usable. The task is left exactly as it was (still
    ``WAITING_FOR_USER``, same ``run_id``, no partial write). Safe to
    retry after a short backoff.

    When the server could not take the answer at all, ``details["accepted"]``
    is ``False``: nothing was written and the reply can be sent again.
    """


# SDK-coined errors (server has no equivalent code).
class XAgentTransportError(XAgentError):
    """A network failure below the HTTP layer, or a response body that
    had already started but ended before reaching an outcome its
    contract allows for.

    Most raises wrap an ``httpx.HTTPError`` (DNS, TLS, connection reset,
    a local timeout below the wall-clock budget); ``http_status`` is
    ``None`` because no HTTP response was received. ``TasksAPI.events()``
    also raises this when the server closes a 200 response cleanly but
    the last frame it delivered was not one of its closing frames --
    httpx reports that as an ordinary end of iteration, not as an
    ``HTTPError``, so the SDK detects and raises it itself.
    """


class MalformedResponse(XAgentError):
    """The HTTP exchange succeeded but the body did not match the shape the
    SDK requires to build a result.

    Raised by the response parsers (e.g. an agent-create body missing its
    ``agent`` block) so the failure points at the response contract rather
    than surfacing as a raw pydantic ``ValidationError`` on a derived
    field. ``http_status`` is ``None`` because the status line itself was
    not the problem -- the decoded payload was. The ``code`` is the
    SDK-coined string ``malformed_response``; the server never emits it,
    so it is absent from ``_CODE_MAP``.

    ``create()``, ``append()`` and ``reply()`` also raise it when their
    202 body does not validate. The server has accepted the write by
    then, so do not resend it blindly: a retried ``create()`` starts a
    second task, and a retried ``append()``/``reply()`` can send the
    message twice.

    Every method that decodes a JSON body also raises it when the response
    status is not the one success status its endpoint sends -- 200, or 202
    for ``create()``, ``append()`` and ``reply()`` -- such as a redirect this
    client does not follow or a 204, and when the success body is not JSON.
    Only the status case carries the status on ``http_status``. This covers
    ``UserClient.me()``, ``agents.*``, ``templates.*``, ``tasks.create()``,
    ``append()``, ``reply()``, ``get()`` and ``steps()``, and the
    ``WorkspaceClient`` methods, as well as the skill methods below.

    ``TasksAPI.events()`` also raises it when the stream's 200 response
    declares a media type other than ``text/event-stream``, or declares
    a charset other than UTF-8 -- SSE bodies are UTF-8 by definition,
    and a mislabeled charset would silently change the text this SDK
    hands back.

    ``TasksAPI.events()`` also raises it when the event-stream endpoint
    answers with a status that is neither an error nor the 200 a stream
    requires -- a redirect this client does not follow, or a 204 -- and
    there it does carry that status, because there the status line is
    the problem.

    The skill methods (``UserClient.skills.list/get/create/upload/update/delete``)
    also raise it when the response status is not 200 (a redirect this
    client does not follow, or a 204), when a 200 body is not JSON, or when
    the JSON does not have the expected shape. Only the status case carries
    the status on ``http_status``. ``skills.list()`` never folds a wrong-shaped
    body into an empty list. ``skills.create()``, ``skills.upload()`` and
    ``skills.update()`` are idempotent, so retrying one after this error is
    safe; a retried ``skills.delete()`` raises ``SkillNotFound`` if the first
    call went through.

    ``UserClient.agents.get_skills()`` and ``replace_skills()`` raise it in the
    same three cases. ``get_skills()`` never folds a wrong-shaped body into an
    empty list. ``replace_skills()`` may already have taken effect when it
    raises this, so call ``get_skills()`` to check what the server stored.

    ``UserClient.mcp_servers.list()``, ``get()`` and ``list_tools()`` raise it
    in the same three cases. ``list()`` never folds a wrong-shaped body into an
    empty list. ``mcp_servers.create()``, ``update()``, ``delete()`` and
    ``set_enabled()`` raise it in the same three cases as well, and also when
    a 200 body does not match the expected shape; the server has accepted the
    write by then. ``update()`` and ``set_enabled()`` can be repeated safely,
    ``delete()`` raises ``ConnectorNotFound`` (or ``ConnectorForbidden`` while
    the team still shares the connector) if the first call went through, and
    a retried ``create()`` raises ``ConnectorNameConflict``.

    ``TaskInfo.parsed()`` and ``RunResult.parsed()`` raise it when the
    structured result fields break the server's contract: a ``"ready"``
    status without an object, or another status without a reason.
    """


class TaskTimeout(XAgentError):
    """A local wall-clock budget elapsed before the operation reached a
    state it was allowed to return.

    Raised by ``TasksAPI.wait`` (and indirectly by ``TasksAPI.run``) when
    the wall-clock budget elapses with the task still in a non-terminal
    state, and by ``TasksAPI.events()`` when its own ``timeout`` budget
    elapses before the stream delivers a closing frame. ``http_status``
    is ``None`` because no HTTP exchange surfaced the failure -- the
    deadline is purely client-side.
    """


class StructuredOutputError(XAgentError):
    """A structured result could not be returned for the run asked about.

    ``code`` says why. It is the server's reason code from
    ``TaskInfo.structured_output_reason`` (see
    ``StructuredOutputReasonCode``), with the server's ``message``, or one
    of the codes only this SDK raises:

      - ``run_mismatch``: the task snapshot shows a run other than
        ``expected_run_id``, or no run. It is raised as the subclass
        ``RunMismatch``, by ``TasksAPI.wait()``, ``TasksAPI.run()``,
        ``TaskInfo.parsed()`` and ``RunResult.parsed()``.
      - ``client_validation_failed``: the ready result does not validate
        with the ``model`` given to ``parsed(model)`` (for example because of
        a ``format`` or length constraint the server does not check); the
        Pydantic ``ValidationError`` is the ``__cause__`` and ``status`` is
        ``"ready"``. A ``model`` that cannot be converted raises
        ``UnsupportedModel`` instead.
      - ``no_structured_output``: ``parsed()`` was called on a snapshot
        without a ``structured_output_status``.

    So one ``except StructuredOutputError`` around ``run()`` and
    ``parsed()`` catches every code above: a run that does not match, a
    reason code from the server, a result the ``model`` rejects
    (``client_validation_failed``) and ``no_structured_output``.
    ``parsed()`` raises ``MalformedResponse`` instead when the fields
    break the server's contract.

    ``status`` is the ``structured_output_status`` the error comes from, or
    ``None`` when it does not come from one. ``missing_fields`` and
    ``unexpressible_fields`` copy the server's lists and are empty when the
    server sent none. ``partial_output`` is a copy of the values the server
    extracted, with ``None`` where a value is missing or cannot be written,
    when ``status`` is ``"incomplete"`` and the snapshot holds such values (the
    task's ``response_format`` sets ``accept_partial``); otherwise ``None``.
    ``http_status`` is ``None``: the error is raised by the SDK after a
    successful response.
    """

    status: str | None
    missing_fields: list[str]
    unexpressible_fields: list[str]
    partial_output: dict[str, Any] | None

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int | None = None,
        *,
        status: str | None = None,
        missing_fields: list[str] | None = None,
        unexpressible_fields: list[str] | None = None,
        partial_output: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code, message, http_status)
        self.status = status
        self.missing_fields = list(missing_fields or [])
        self.unexpressible_fields = list(unexpressible_fields or [])
        self.partial_output = partial_output


class RunMismatch(StructuredOutputError):
    """Code ``run_mismatch``: the task snapshot shows a run other than the
    one asked about.

    Raised by ``TasksAPI.wait()`` when it is given ``expected_run_id`` and
    the snapshot it is about to return (terminal or ``WAITING_FOR_USER``)
    shows another run or no run, and so by ``TasksAPI.run()``, which passes
    the run it created. Polling stops and the snapshot is not returned;
    call ``get(task_id)`` to read it. ``TaskInfo.parsed()`` raises it when
    it is given ``expected_run_id`` and the snapshot shows another run or
    no run, and so does ``RunResult.parsed()``, which passes the run
    ``run()`` started. The task can show another run, or no run, because:

      - another caller appended a turn after yours finished;
      - the server refused to start your turn: the task still shows the
        previous run's result, or, for a new task, ``FAILED`` with no run
        (``actual_run_id`` is ``None``);
      - the server is older than v0.8.5 and your appended turn is still
        queued: such a server shows the previous turn until it starts.

    ``task_id``, ``expected_run_id`` and ``actual_run_id`` say which task,
    which run was asked about and which run the snapshot shows.
    It is a ``StructuredOutputError``, so ``except StructuredOutputError``
    catches it too; its ``status`` is ``None`` and its ``missing_fields``
    and ``unexpressible_fields`` are empty.
    ``http_status`` is ``None``: the error is raised by the SDK after a
    successful response.
    """

    task_id: int | None
    expected_run_id: str | None
    actual_run_id: str | None

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int | None = None,
        *,
        task_id: int | None = None,
        expected_run_id: str | None = None,
        actual_run_id: str | None = None,
    ) -> None:
        super().__init__(code, message, http_status)
        self.task_id = task_id
        self.expected_run_id = expected_run_id
        self.actual_run_id = actual_run_id


def _run_mismatch(task_id: int, expected: str, actual: str | None) -> RunMismatch:
    """The error for a snapshot of ``task_id`` that shows another run."""
    shown = "no run" if actual is None else f"run {actual!r}"
    return RunMismatch(
        "run_mismatch",
        f"Task {task_id} shows {shown}, not the expected run {expected!r}: "
        "another caller's turn replaced it, or the expected run was not "
        "started (refused, or still queued on a server older than v0.8.5).",
        task_id=task_id,
        expected_run_id=expected,
        actual_run_id=actual,
    )


class UnsupportedFeature(XAgentError):
    """The server, or the key in use, does not support a feature the call
    needs. Code ``unsupported_feature``.

    Raised by ``TasksAPI.create()``, ``append()`` and ``run()`` when called
    with a ``response_format`` and the server cannot honor it:

      - before anything is created, when ``GET /v1/features`` answers 404 (a
        server that predates structured output) or does not list
        ``structured_output`` (a key that cannot set a format). ``task_id`` is
        ``None``.
      - after the server accepted the request, when its response does not
        echo back the format that was sent. ``task_id`` is the task the
        request created or appended to: it exists, but do not rely on it
        having that format.

    ``http_status`` is ``None``: the error is decided by the SDK.
    """

    task_id: int | None

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int | None = None,
        *,
        task_id: int | None = None,
    ) -> None:
        super().__init__(code, message, http_status)
        self.task_id = task_id


class UnsupportedSchema(XAgentError):
    """A ``response_format`` the SDK refuses before sending any request.
    Code ``unsupported_schema``.

    The value breaks one of the server's rules that the SDK checks
    locally: ``rule`` is the rule id (for example ``"S3"``), ``path`` the
    rendered position (``"/properties/a/minLength"``, or ``"response_format"``
    / ``"the schema root"`` for the root), and ``message`` exactly the server's
    message for it: ``<rule> at <path>: <text>``. ``http_status`` is
    ``None``: nothing was sent.

    A Pydantic model that cannot be converted into the supported schema
    subset raises the subclass ``UnsupportedModel`` instead, so
    ``except UnsupportedSchema`` catches both.
    """

    rule: str
    path: str

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int | None = None,
        *,
        rule: str,
        path: str,
    ) -> None:
        super().__init__(code, message, http_status)
        self.rule = rule
        self.path = path


class UnsupportedModel(UnsupportedSchema):
    """A Pydantic model, passed as ``response_format`` or to
    ``parsed(model)``, that the SDK cannot convert into the supported schema
    subset. Code ``unsupported_schema``.

    The model uses something the subset cannot express (``dict[str, X]``,
    ``Any``, a fixed-length tuple, a union of several non-null types, a
    recursive model, ``Decimal``, a ``Literal`` mixing value types), or a
    constraint that cannot be written into the description (a ``format``
    outside ``[a-z0-9-]+``, or constraints that make a description longer
    than the server accepts). ``create()``, ``append()`` and ``run()`` raise
    it before any request; ``parsed(model)`` raises it instead of reading the
    result.

    A refused conversion names no server rule, so ``rule`` is always
    ``"model"``, which is not a server rule id; ``rule`` stays a ``str`` for
    code that handles every ``UnsupportedSchema``. ``path`` is the position
    where the conversion stopped, as a JSON Pointer from the root of the
    model's JSON schema (a
    field such as ``"/properties/data"``, a keyword such as
    ``"/properties/day/format"``, or a ``$defs`` entry under the model's own
    name such as ``"/$defs/Node"``), or ``"the schema root"``. A nested model
    used with a ``description`` is reported under the field that uses it, and
    a keyword in the value branch of a scalar ``Optional`` on the field
    itself. ``message`` names that position and the reason, and for most of
    them what to write instead. ``http_status`` is ``None``: the SDK raises
    it, not the server.
    """

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int | None = None,
        *,
        path: str,
    ) -> None:
        super().__init__(code, message, http_status, rule="model", path=path)


_CODE_MAP: dict[str, type[XAgentError]] = {
    "invalid_api_key": InvalidAPIKey,
    "agent_not_found": AgentNotFound,
    "task_not_found": TaskNotFound,
    "task_busy": TaskBusy,
    "invalid_input": InvalidInput,
    "template_not_found": TemplateNotFound,
    "rate_limited": RateLimited,
    "internal_error": InternalError,
    "interaction_response_required": InteractionResponseRequired,
    "no_pending_interaction": NoPendingInteraction,
    "interaction_not_resumable": InteractionNotResumable,
    "temporarily_unavailable": TemporarilyUnavailable,
    "api_key_not_found": APIKeyNotFound,
    "skill_not_found": SkillNotFound,
    "skill_already_exists": SkillAlreadyExists,
    "connector_not_found": ConnectorNotFound,
    "transport_not_supported": TransportNotSupported,
    "connector_unavailable": ConnectorUnavailable,
    "connector_tools_unavailable": ConnectorToolsUnavailable,
    "mcp_oauth_authorization_failed": McpOAuthAuthorizationFailed,
    "connector_name_conflict": ConnectorNameConflict,
    "connector_not_editable": ConnectorNotEditable,
    "connector_forbidden": ConnectorForbidden,
}


def from_response(resp: httpx.Response) -> XAgentError:
    """Map a 4xx/5xx httpx.Response to the appropriate XAgentError subclass.

    Caller must have already confirmed ``resp.status_code >= 400``.

    Recognizes:
      - V1 envelope: ``{"error": {"code": ..., "message": ...}}``, with an
        optional ``details`` object that is kept on the exception (a
        ``details`` value that is not an object is dropped); the
        ``Retry-After`` response header is kept as ``retry_after`` when it
        is a whole number of seconds
      - Legacy FastAPI validation fallback: HTTP 422 with ``{"detail": [...]}``
        -> InvalidInput
      - Anything else (non-JSON body, missing ``error`` key, unknown ``code``)
        falls back to InternalError with the raw body as the message.
    """
    status = resp.status_code

    try:
        body: Any = resp.json()
    except ValueError:
        return InternalError("internal_error", _fallback_message(resp.text), status)

    error_obj = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error_obj, dict):
        if status == 422:
            detail = body.get("detail") if isinstance(body, dict) else None
            return InvalidInput("invalid_input", _format_422_detail(detail), status)
        return InternalError("internal_error", _fallback_message(body), status)

    code = error_obj.get("code")
    message = error_obj.get("message", "")
    if not isinstance(code, str) or not isinstance(message, str):
        return InternalError("internal_error", _fallback_message(body), status)

    details = error_obj.get("details")
    cls = _CODE_MAP.get(code, InternalError)
    return cls(
        code,
        message,
        status,
        details=details if isinstance(details, dict) else None,
        retry_after=_parse_retry_after(resp.headers.get("retry-after")),
    )


def _parse_retry_after(value: str | None) -> int | None:
    """Read a ``Retry-After`` header given as whole seconds.

    Only one to nine ASCII digits count; HTTP dates, signs, fractions and
    lists are ignored (None). ``0`` is a valid answer.
    """
    if value is None or re.fullmatch(r"[0-9]{1,9}", value) is None:
        return None
    return int(value)


def _fallback_message(value: Any) -> str:
    """Normalize fallback error payloads before exposing them on exceptions."""
    message = value if isinstance(value, str) else str(value)
    if not message:
        message = "<empty body>"
    if len(message) > _MAX_FALLBACK_ERROR_MESSAGE_LENGTH:
        return message[:_MAX_FALLBACK_ERROR_MESSAGE_LENGTH] + "..."
    return message


def _format_422_detail(detail: Any) -> str:
    """Compact FastAPI's ``detail`` list into a single human-readable string."""
    if not detail:
        return "validation failed"
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        return "; ".join(_format_422_item(i) for i in detail)
    return str(detail)


def _format_422_item(item: Any) -> str:
    """Compact one FastAPI 422 detail entry into ``loc.path: msg`` form.

    FastAPI typically emits ``{"loc": [...], "msg": "...", "type": "..."}``
    per entry; preserving ``loc`` is more useful than ``msg`` alone
    because it tells the caller which field failed. Falls back to ``msg``
    when ``loc`` is missing, or to ``str(item)`` when neither field is
    a string.
    """
    if not isinstance(item, dict):
        return str(item)
    msg = item.get("msg")
    loc = item.get("loc")
    if isinstance(msg, str) and isinstance(loc, list) and loc:
        return f"{'.'.join(str(p) for p in loc)}: {msg}"
    if isinstance(msg, str):
        return msg
    return str(item)
