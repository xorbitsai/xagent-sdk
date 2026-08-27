"""Test-only helpers for building SSE (server-sent-events) mock bodies.

``TasksAPI.events()`` is tested exclusively against ``httpx.MockTransport``
(see ``conftest.make_client``); these helpers build the raw wire bytes a
real server would send, so tests exercise the SDK's own SSE parser
rather than any shortcut through it.
"""

import json
from collections.abc import Iterable, Iterator
from typing import Any, Protocol

import httpx


class _ClockLike(Protocol):
    def advance(self, seconds: float) -> None: ...


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
    channel other than the byte stream itself. ``close_exc``, if given,
    is raised from ``close()`` after the count is still recorded --
    for a test that needs a clean read followed by a *failing* close,
    as opposed to ``RaisingByteStream``, which always fails the read
    too.
    """

    def __init__(
        self, chunks: Iterable[bytes], *, close_exc: Exception | None = None
    ) -> None:
        self._chunks = list(chunks)
        self.close_count = 0
        self._close_exc = close_exc

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks

    def close(self) -> None:
        self.close_count += 1
        if self._close_exc is not None:
            raise self._close_exc


class RaisingByteStream(httpx.SyncByteStream):
    """Yields some bytes, then raises ``exc`` instead of ending cleanly.

    Simulates a connection that breaks (or times out) partway through a
    response body -- something a canned ``bytes`` payload cannot do.
    Also counts ``close()`` calls (see ``CloseRecordingStream``) so a
    test can confirm the connection was released exactly once even on
    this failing path. ``raise_count`` counts how many times ``exc`` was
    actually raised out of ``__iter__`` -- a test asserting the SDK
    reached the code path that reads this exception (rather than
    short-circuiting before ever calling ``next()`` on this stream, e.g.
    because a wall-clock checkpoint fired first) checks this instead of
    just the exception type it eventually observes, since the wrong
    code path can raise the same exception type for the wrong reason.

    ``clock`` and ``advance_before_raise`` (shape borrowed from
    ``ClockAdvancingStream``) advance a fake clock immediately before
    raising, rather than the test doing it beforehand: a test that
    needs the deadline to elapse *during* the read that hits ``exc`` --
    not already elapsed before that read is even attempted -- cannot
    build that timing by calling ``clock.advance()`` before iterating,
    since a proactive checkpoint would then fire first and ``exc``
    would never actually be raised.
    """

    def __init__(
        self,
        chunks: Iterable[bytes],
        exc: Exception,
        *,
        close_exc: Exception | None = None,
        clock: _ClockLike | None = None,
        advance_before_raise: float = 0.0,
    ) -> None:
        self._chunks = list(chunks)
        self._exc = exc
        self._close_exc = close_exc
        self._clock = clock
        self._advance_before_raise = advance_before_raise
        self.close_count = 0
        self.raise_count = 0

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks
        if self._clock is not None:
            self._clock.advance(self._advance_before_raise)
        self.raise_count += 1
        raise self._exc

    def close(self) -> None:
        self.close_count += 1
        if self._close_exc is not None:
            raise self._close_exc


class ClockAdvancingStream(httpx.SyncByteStream):
    """Yields ``chunks`` one at a time, advancing a fake clock by
    ``interval`` before each one instead of sleeping.

    Pairs with the ``clock`` fixture in ``test_events.py``: a test that
    needs a wall-clock deadline to fire against a stream that otherwise
    stays "healthy" (steady heartbeats) can drive that clock through
    ``clock.advance()`` between chunks -- deterministic and instant --
    without a real ``time.sleep()``. ``interval=0`` advances the clock
    by nothing on each pull, leaving a test free to call
    ``clock.advance()`` itself between reads instead.

    ``sent`` counts how many chunks have actually been pulled off the
    stream so far -- not how many were handed to the constructor. A
    test asserting a deadline fired *before* the next read (rather than
    merely before that read finished) checks ``sent`` stayed at the
    count it had after the last frame it saw, not that the read failed
    or was interrupted midway.
    """

    def __init__(
        self, chunks: Iterable[bytes], clock: _ClockLike, interval: float
    ) -> None:
        self._chunks = list(chunks)
        self._clock = clock
        self._interval = interval
        self.sent = 0

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._chunks:
            self._clock.advance(self._interval)
            self.sent += 1
            yield chunk


def frame(event: str, data: dict[str, Any] | str, *, sep: str = "\n") -> str:
    """One well-formed SSE frame: ``event:``, ``data:``, then a blank line.

    ``data`` is JSON-encoded unless already given as a literal string
    (for building deliberately malformed bodies), with
    ``ensure_ascii=False`` -- matching the server's own serializer, so
    non-ASCII text lands on the wire as raw UTF-8 bytes rather than
    ``\\uXXXX`` escapes a test builder introduced on its own. ``sep`` is
    the line terminator and defaults to the server's own: it emits
    ``\\n`` (``shared/README.md`` documents that wire form). ``\\r\\n``
    is equally legal SSE, and a test that wants that variant passes it
    explicitly.
    """
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}{sep}data: {payload}{sep}{sep}"


def ping(*, sep: str = "\n") -> str:
    """One heartbeat comment frame, matching the server's ``: ping``.

    Same default as ``frame()``: the server terminates it with ``\\n``.
    """
    return f": ping{sep}{sep}"


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

    def set_handler(self, handler: Any) -> None:
        """Swap in a handler built after this transport already exists.

        A handler that itself needs to reference this transport (e.g.
        to build a ``TransportAwareStream`` bound to it) cannot be
        constructed before the transport is, so the constructor
        argument is a chicken-and-egg problem for that case -- build
        the transport with a placeholder, then call this once the real
        handler closure exists.
        """
        self._handler = handler

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
