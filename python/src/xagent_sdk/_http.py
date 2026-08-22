from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Any, Self

import httpx

from xagent_sdk._version import __version__
from xagent_sdk.errors import XAgentTransportError

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_CONNECT_TIMEOUT = 10.0
_DEFAULT_MAX_CONNECTIONS = 10

# Timeout legs for the task event stream (``stream_lines``). ``connect``
# mirrors _DEFAULT_CONNECT_TIMEOUT -- establishing the connection is the
# same operation whether or not the request ends up streaming. ``write``
# and ``pool`` have no stream-specific meaning (this is a GET with no
# body, and a streaming response is not held in the connection pool --
# see the ``_events`` module docstring) but are still given finite
# values so no leg is left as httpx's ``None`` ("wait forever").
_STREAM_CONNECT_TIMEOUT = 10.0
_STREAM_WRITE_TIMEOUT = 10.0
_STREAM_POOL_TIMEOUT = 10.0


def _build_user_agent() -> str:
    return f"xagent-sdk-python/{__version__} (httpx/{httpx.__version__})"


class HTTPClient:
    """Thin httpx.Client wrapper for the xAgent v1 API.

    Owns a single httpx.Client (connection pool) for the lifetime of the
    enclosing public client (AgentClient or UserClient). Returns raw
    httpx.Response objects; HTTP-status-to-exception mapping is layered
    on top in ``_BaseClient._request``.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        max_connections: int = _DEFAULT_MAX_CONNECTIONS,
        user_agent: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must be a non-empty string")
        if not base_url:
            raise ValueError("base_url must be a non-empty string")

        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": user_agent or _build_user_agent(),
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(timeout, connect=_DEFAULT_CONNECT_TIMEOUT),
            limits=httpx.Limits(max_connections=max_connections),
            transport=transport,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            return self._client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise XAgentTransportError(
                "transport_error", str(exc), http_status=None
            ) from exc

    @contextmanager
    def stream_lines(
        self, method: str, path: str, *, read_timeout: float
    ) -> Iterator[tuple[httpx.Response, Iterator[str]]]:
        """Open a server-sent-events connection and yield the response
        together with a line iterator over its body.

        Uses the existing ``self._client`` connection pool -- streaming
        responses do not get a dedicated ``httpx.Client`` (see the
        ``_events`` module docstring for why). Overrides the client's
        default ``Accept: application/json`` for this one request, and
        overrides its timeout: ``read_timeout`` is the caller's
        already-computed budget, the other three legs are fixed
        transport configuration unrelated to the stream's semantics.

        Unlike ``request()``, this does not catch ``httpx.HTTPError``:
        any failure while establishing the connection (DNS/TLS/connect
        failures, or a ``ReadTimeout`` while still waiting for response
        headers) propagates to the caller unwrapped. Classifying it is
        the caller's job -- only the caller (``_events.py``) knows the
        wall-clock budget needed to tell a timed-out wait apart from a
        genuine transport failure.
        """
        request_timeout = httpx.Timeout(
            connect=_STREAM_CONNECT_TIMEOUT,
            read=read_timeout,
            write=_STREAM_WRITE_TIMEOUT,
            pool=_STREAM_POOL_TIMEOUT,
        )
        with self._client.stream(
            method,
            path,
            headers={"Accept": "text/event-stream"},
            timeout=request_timeout,
        ) as resp:
            yield resp, resp.iter_lines()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()
