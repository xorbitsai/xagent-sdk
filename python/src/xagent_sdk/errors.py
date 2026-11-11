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
    """

    code: str
    message: str
    http_status: int | None

    def __init__(self, code: str, message: str, http_status: int | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.http_status = http_status


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
    """


class RateLimited(XAgentError):
    """HTTP 429, code ``rate_limited``."""


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
    """The result of the run asked about could not be returned.

    ``code`` says why. It is a code only this SDK raises:

      - ``run_mismatch``: the task snapshot belongs to a run other than
        ``expected_run_id``. Raised by ``TasksAPI.wait()`` and
        ``TasksAPI.run()``.

    ``http_status`` is ``None``: the error is raised by the SDK after a
    successful response.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, http_status=None)


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
    "skill_not_found": SkillNotFound,
    "skill_already_exists": SkillAlreadyExists,
}


def from_response(resp: httpx.Response) -> XAgentError:
    """Map a 4xx/5xx httpx.Response to the appropriate XAgentError subclass.

    Caller must have already confirmed ``resp.status_code >= 400``.

    Recognizes:
      - V1 envelope: ``{"error": {"code": ..., "message": ...}}``
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

    cls = _CODE_MAP.get(code, InternalError)
    return cls(code, message, status)


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
