"""Test-only helpers for building SSE (server-sent-events) mock bodies.

``TasksAPI.events()`` is tested exclusively against ``httpx.MockTransport``
(see ``conftest.make_client``); these helpers build the raw wire bytes a
real server would send, so tests exercise the SDK's own SSE parser
rather than any shortcut through it.
"""

import json
import time
from collections.abc import Iterable, Iterator
from typing import Any

import httpx


class RawByteStream(httpx.SyncByteStream):
    """Feeds pre-built byte chunks to ``httpx.Response(stream=...)``.

    Deliberately a real ``httpx.SyncByteStream``, never ``content=`` or
    ``json=``: those two pre-fill ``Response._content`` at construction
    time, so ``.json()`` (or in this module's case, iterating the body)
    would work *before* ``.read()``/streaming is engaged -- masking a
    bug where the SDK forgets to treat the response as a stream. See
    ``TestHttpErrorMapping`` in ``test_events.py`` for the case this
    guards.
    """

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = list(chunks)

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks


class CloseRecordingStream(httpx.SyncByteStream):
    """A ``RawByteStream`` that also counts ``close()`` calls.

    Lets a test observe that the SDK really released the underlying
    response body -- and released it exactly once -- with no side
    channel other than the byte stream itself.
    """

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = list(chunks)
        self.close_count = 0

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks

    def close(self) -> None:
        self.close_count += 1


class RaisingByteStream(httpx.SyncByteStream):
    """Yields some bytes, then raises ``exc`` instead of ending cleanly.

    Simulates a connection that breaks (or times out) partway through a
    response body -- something a canned ``bytes`` payload cannot do.
    Also counts ``close()`` calls (see ``CloseRecordingStream``) so a
    test can confirm the connection was released exactly once even on
    this failing path.
    """

    def __init__(self, chunks: Iterable[bytes], exc: Exception) -> None:
        self._chunks = list(chunks)
        self._exc = exc
        self.close_count = 0

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks
        raise self._exc

    def close(self) -> None:
        self.close_count += 1


class DelayedChunksStream(httpx.SyncByteStream):
    """Yields ``chunks`` one at a time, sleeping ``interval`` real
    wall-clock seconds before each one.

    ``httpx.MockTransport`` does not enforce read timeouts on its own --
    a stub that merely calls ``time.sleep()`` inside a single ``__iter__``
    body still returns to the caller in one shot with no ``ReadTimeout``.
    A test that needs a wall-clock deadline to actually observe elapsed
    time (e.g. a stream that must eventually time out while an
    otherwise-healthy peer keeps sending heartbeats) needs the sleep to
    happen *between* chunks the SDK reads one at a time, which is what
    this does.
    """

    def __init__(self, chunks: Iterable[bytes], interval: float) -> None:
        self._chunks = list(chunks)
        self._interval = interval

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._chunks:
            time.sleep(self._interval)
            yield chunk


def frame(event: str, data: dict[str, Any] | str) -> str:
    """One well-formed SSE frame: ``event:``, ``data:``, then a blank line.

    ``data`` is JSON-encoded unless already given as a literal string
    (for building deliberately malformed bodies).
    """
    payload = data if isinstance(data, str) else json.dumps(data)
    return f"event: {event}\r\ndata: {payload}\r\n\r\n"


def ping() -> str:
    """One heartbeat comment frame, matching the server's ``: ping``."""
    return ": ping\r\n\r\n"


def body(*parts: str) -> bytes:
    """Join frames/comments into the raw bytes a stream response sends."""
    return "".join(parts).encode()


def stream_response(
    *parts: str,
    status: int = 200,
    content_type: str | None = "text/event-stream",
) -> httpx.Response:
    """Build a streaming ``httpx.Response`` from SSE frame text."""
    headers = {} if content_type is None else {"content-type": content_type}
    return httpx.Response(status, headers=headers, stream=RawByteStream([body(*parts)]))


def error_response(status: int, error_body: dict[str, Any]) -> httpx.Response:
    """Build a 4xx/5xx response carrying a V1 error envelope.

    Uses ``RawByteStream``, not ``json=``, for the same reason
    documented on ``RawByteStream``.
    """
    return httpx.Response(
        status, stream=RawByteStream([json.dumps(error_body).encode()])
    )


def step_payload(
    id_: str,
    *,
    type_: str = "tool_call",
    status: str = "completed",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal, schema-valid ``PublicStep`` payload for a
    ``step.started`` / ``step.completed`` frame's ``data["step"]``.
    """
    if data is None:
        data = {
            "tool_call": {"name": "execute_python_code"},
            "agent_delegation": {"sub_agent_name": "ChartCraft"},
            "thinking": {"phase": "planning"},
            "message": {"role": "assistant", "content": "hi"},
        }[type_]
    return {
        "id": id_,
        "type": type_,
        "status": status,
        "started_at": "2026-05-10T03:00:00Z",
        "completed_at": "2026-05-10T03:00:01Z" if status == "completed" else None,
        "data": data,
    }


class ClosableTransport(httpx.BaseTransport):
    """A transport whose ``close()`` flips a flag an in-flight stream
    can observe.

    ``httpx.MockTransport.close()`` is a no-op (it holds no real
    connections to release), so it cannot reproduce what closing a real
    ``AgentClient`` does to *other* streams still reading from the same
    connection pool. This transport lets a test simulate that by having
    ``TransportAwareStream`` check ``.closed`` before handing over its
    next chunk.
    """

    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.closed = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.read()
        response: httpx.Response = self._handler(request)
        return response

    def close(self) -> None:
        self.closed = True


class TransportAwareStream(httpx.SyncByteStream):
    """Yields ``chunks`` in order, but raises ``httpx.ReadError`` instead
    of yielding the next one once ``transport.closed`` is true.

    Pairs with ``ClosableTransport`` to simulate a connection that
    breaks when the owning ``AgentClient`` is closed.
    """

    def __init__(self, transport: ClosableTransport, chunks: Iterable[bytes]) -> None:
        self._transport = transport
        self._chunks = list(chunks)

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._chunks:
            if self._transport.closed:
                raise httpx.ReadError("connection closed")
            yield chunk
