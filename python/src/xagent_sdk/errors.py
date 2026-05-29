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
    """HTTP 409, code ``task_busy``. Only retryable code (caller decides)."""


class RateLimited(XAgentError):
    """HTTP 429, code ``rate_limited``."""


class InternalError(XAgentError):
    """HTTP 500, code ``internal_error``.

    Also used as the fallback for unknown codes or malformed error bodies.
    """


class InvalidInput(XAgentError):
    """HTTP 422, code ``invalid_input``.

    Current ``/v1/*`` endpoints return this through the V1 envelope.
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


# SDK-coined errors (server has no equivalent code).
class XAgentTransportError(XAgentError):
    """Network, DNS, TLS, or local timeout below the HTTP layer.

    Wraps any ``httpx.HTTPError``. ``http_status`` is None because no HTTP
    response was received.
    """


class TaskTimeout(XAgentError):
    """``wait()`` / ``run()`` exceeded its local deadline waiting for a task
    to reach a terminal state.

    Raised by ``TasksAPI.wait`` (and indirectly by ``TasksAPI.run``) when
    the wall-clock budget elapses with the task still in a non-terminal
    state. ``http_status`` is ``None`` because no HTTP exchange surfaced
    the failure -- the deadline is purely client-side.
    """


_CODE_MAP: dict[str, type[XAgentError]] = {
    "invalid_api_key": InvalidAPIKey,
    "agent_not_found": AgentNotFound,
    "task_not_found": TaskNotFound,
    "task_busy": TaskBusy,
    "invalid_input": InvalidInput,
    "template_not_found": TemplateNotFound,
    "rate_limited": RateLimited,
    "internal_error": InternalError,
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
