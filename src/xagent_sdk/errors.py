from typing import Any

import httpx


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
    """HTTP 429, code ``rate_limited``. Reserved; backend does not yet emit."""


class InternalError(XAgentError):
    """HTTP 500, code ``internal_error``.

    Also used as the fallback for unknown codes or malformed error bodies.
    """


# SDK-coined errors (server has no equivalent code).
class InvalidInput(XAgentError):
    """HTTP 422 from FastAPI request validation.

    FastAPI uses ``{"detail": [...]}`` instead of the V1 envelope; the SDK
    coins the code ``invalid_input`` for uniformity.
    """


class XAgentTransportError(XAgentError):
    """Network, DNS, TLS, or local timeout below the HTTP layer.

    Wraps any ``httpx.HTTPError``. ``http_status`` is None because no HTTP
    response was received.
    """


class TaskTimeout(XAgentError):
    """``wait()`` / ``run()`` exceeded its local deadline waiting for a task
    to reach a terminal state.

    Not yet raised by any code path; reserved for the polling layer added
    in a later commit.
    """


_CODE_MAP: dict[str, type[XAgentError]] = {
    "invalid_api_key": InvalidAPIKey,
    "agent_not_found": AgentNotFound,
    "task_not_found": TaskNotFound,
    "task_busy": TaskBusy,
    "rate_limited": RateLimited,
    "internal_error": InternalError,
}


def from_response(resp: httpx.Response) -> XAgentError:
    """Map a 4xx/5xx httpx.Response to the appropriate XAgentError subclass.

    Caller must have already confirmed ``resp.status_code >= 400``.

    Recognizes:
      - V1 envelope: ``{"error": {"code": ..., "message": ...}}``
      - FastAPI validation: HTTP 422 with ``{"detail": [...]}`` -> InvalidInput
      - Anything else (non-JSON body, missing ``error`` key, unknown ``code``)
        falls back to InternalError with the raw body as the message.
    """
    status = resp.status_code

    try:
        body: Any = resp.json()
    except ValueError:
        return InternalError("internal_error", resp.text or "<empty body>", status)

    if status == 422:
        detail = body.get("detail") if isinstance(body, dict) else None
        return InvalidInput("invalid_input", _format_422_detail(detail), status)

    error_obj = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error_obj, dict):
        return InternalError("internal_error", str(body), status)

    code = error_obj.get("code")
    message = error_obj.get("message", "")
    if not isinstance(code, str) or not isinstance(message, str):
        return InternalError("internal_error", str(body), status)

    cls = _CODE_MAP.get(code, InternalError)
    return cls(code, message, status)


def _format_422_detail(detail: Any) -> str:
    """Compact FastAPI's ``detail`` list into a single human-readable string."""
    if not detail:
        return "validation failed"
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        return "; ".join(str(item) for item in detail)
    return str(detail)
