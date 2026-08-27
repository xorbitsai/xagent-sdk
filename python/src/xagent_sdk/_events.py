"""Server-sent-events consumption for ``AgentClient.tasks.events()``.

The wire format is the v1 task event stream (``GET
/v1/chat/tasks/{task_id}/events``, ``Content-Type: text/event-stream``):
one frame per named event (an ``event: <name>`` line, a ``data:
<json>`` line, then a blank line), plus an occasional ``: ping``
comment line as a keep-alive. There are 8 event names; three of them --
``task.completed``, ``task.input_required``, ``stream.error`` -- are
closing frames. When the server attaches to an already-finished task it
sends that attach's one-shot step snapshot *before* the conclusion
frame, and the only frame that can follow a conclusion is a
``stream.error`` naming why the snapshot is incomplete -- so "the last
frame this stream delivered" is the fact this module tracks, not "stop
at the first closing frame seen": a ``[conclusion, stream.error]``
close must resolve to the ``stream.error``.

Streaming shares the enclosing client's single ``httpx.Client``, and so
shares that client's connection pool: an open stream holds one of the
pool's ``max_connections`` slots for as long as it stays open, and an
ordinary request that finds every slot taken waits out its pool timeout
and then fails with ``XAgentTransportError``. The server allows 2
concurrent streams per task and 32 per API key; a caller that wants to
run several at once has to raise ``max_connections`` (default 10) to
cover the streams plus whatever headroom its ordinary calls need. This
release has no separate pool for streams and no admission control --
the pool size is the only knob. A release that fails to close can hold
its slot for the life of the client (see ``TaskEventStream.close()``);
a failure on a path that cannot re-raise it is logged instead of being
silently dropped, both before a ``TaskEventStream`` exists to own the
release (``open_task_event_stream()``'s own open-time cleanup) and
after (see ``TaskEventStream._close_quietly()``).

Only a frame's ``event`` name is ever branch-matched on in this module.
A ``stream.error`` frame's ``message`` text is not part of the wire
contract and can change between server releases without notice; callers
must do the same and branch on ``data.get("code")`` only (``.get()``
because a closing frame whose body never arrived carries empty
``data``).
"""

from __future__ import annotations

import codecs
import contextlib
import json
import logging
import math
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field

import httpx
from pydantic import ValidationError

from xagent_sdk._http import (
    _STREAM_CONNECT_TIMEOUT,
    _STREAM_POOL_TIMEOUT,
    _STREAM_WRITE_TIMEOUT,
    HTTPClient,
)
from xagent_sdk.errors import (
    MalformedResponse,
    TaskTimeout,
    XAgentTransportError,
    from_response,
)
from xagent_sdk.types import _STEP_ADAPTER, Step, StreamEvent, StreamEventType

logger = logging.getLogger(__name__)

# How long events() will wait for the next byte (including a heartbeat)
# before treating the connection as dead. Bounded by the caller's own
# timeout when that is smaller -- see open_task_event_stream().
_STREAM_READ_TIMEOUT = 60.0


def _clamped_leg(ceiling: float, timeout: float | None) -> float:
    """One httpx timeout leg, never outlasting the caller's budget.

    With a positive ``timeout``, a blocking phase gets the smaller of
    its own ceiling and the whole budget, so a budget spent stuck
    connecting or stuck waiting for a free pool slot expires as a
    ``TaskTimeout`` instead of running the fixed ceiling out first.
    ``timeout is None`` means no budget, and ``timeout == 0`` keeps the
    full ceilings on purpose: that value is documented as "open the
    connection once, then raise", not "raise instantly".
    """
    if timeout is None or timeout <= 0.0:
        return ceiling
    return min(timeout, ceiling)


# The server silently drops an inbound broadcast frame once its own
# measured text exceeds 262144 characters. This SDK-side cap on a
# single assembled frame is 4x that -- comfortably above any frame the
# server would ever forward -- so it only ever fires on a frame the
# server should not have sent (or a proxy/bug duplicating data: lines
# without bound), and it exists purely to bound this module's memory
# use while resynchronizing at the next blank line.
_MAX_FRAME_CHARS = 1_048_576

_CLOSE_EVENT_NAMES = frozenset(
    {
        StreamEventType.TASK_COMPLETED.value,
        StreamEventType.TASK_INPUT_REQUIRED.value,
        StreamEventType.STREAM_ERROR.value,
    }
)
_STREAM_ERROR_NAME = StreamEventType.STREAM_ERROR.value
_STEP_EVENT_NAMES = frozenset(
    {StreamEventType.STEP_STARTED.value, StreamEventType.STEP_COMPLETED.value}
)
_KNOWN_EVENT_NAMES = frozenset(member.value for member in StreamEventType)


# --- SSE line assembly --------------------------------------------------


@dataclass(frozen=True)
class _RawFrame:
    """One assembled SSE frame before JSON decoding: an event name (or
    ``None`` if the frame had no ``event:`` line) and the joined text of
    every ``data:`` line, in order.
    """

    event: str | None
    data: str


class _Ping:
    """Sentinel: a comment line (the server's ``: ping`` keep-alive) was
    consumed. It produces no frame and advances nothing -- neither the
    wall-clock deadline nor any liveness window. What keeps a silent
    connection from hanging is the per-read timeout in
    ``open_task_event_stream()``; a ping simply arrives before that
    window elapses.
    """


class _Oversized:
    """Sentinel: the frame in progress exceeded ``_MAX_FRAME_CHARS`` and
    was discarded; the lines that belonged to it are already consumed.
    """


_PING = _Ping()
_OVERSIZED = _Oversized()


@dataclass
class _FrameBuilder:
    event: str | None = None
    data_parts: list[str] = field(default_factory=list)
    char_count: int = 0
    oversized: bool = False
    saw_field_line: bool = False


class _FrameAssembler:
    """Groups SSE lines into frames, one line at a time.

    Implements the parts of the SSE wire format the server's frames
    actually use, plus enough of the general field-line grammar to stay
    forward compatible with fields it does not use today:

      - A blank line dispatches whatever has been accumulated.
      - ``event: <name>`` sets the frame's event name.
      - ``data: <text>`` appends to the frame's data buffer; per the SSE
        spec, multiple ``data:`` lines join with ``"\\n"`` (the server
        always emits a single line, but a hand-built test frame may not).
      - Any other field name (``id:``, ``retry:``, or one this module
        does not know about) is a recognized field line and is ignored;
        the rest of the frame around it is still assembled normally. A
        frame built entirely out of such lines (no ``event:``, no
        ``data:``) is still a frame -- it is dispatched with no event
        name and dropped downstream by ``_parse_frame``'s "no ``event:``
        line" reason, counted like any other discarded frame, rather
        than vanishing silently.
      - A line starting with ``:`` is a comment -- the server's
        heartbeat. It never contributes to a frame; ``feed()`` reports it
        as ``_PING`` immediately rather than waiting for a frame to
        complete.
      - A frame whose accumulated ``data:`` character count exceeds
        ``_MAX_FRAME_CHARS`` is abandoned: further lines belonging to it
        are consumed without buffering, and ``_OVERSIZED`` is reported
        once the terminating blank line arrives.
      - A partially accumulated frame that never receives its
        terminating blank line (i.e. the stream hit EOF mid-frame) is
        simply never dispatched -- the caller does not feed a final
        blank line on EOF, so that half-frame is silently discarded by
        omission, not by any code path here.
      - The stream's very first line has a leading U+FEFF byte-order
        mark stripped, if present, before anything else looks at it.
    """

    def __init__(self) -> None:
        self._builder = _FrameBuilder()
        self._at_stream_start = True

    def feed(self, line: str) -> _RawFrame | _Ping | _Oversized | None:
        if self._at_stream_start:
            self._at_stream_start = False
            # Per the SSE spec one leading U+FEFF belongs to the body's
            # encoding, not to the first field name. httpx decodes as
            # plain utf-8 (not utf-8-sig), so without this the first
            # line's name is "﻿event" and the whole first frame is
            # dropped as an unrecognized field.
            line = line.removeprefix("﻿")
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return _PING
        # A comment line (above) never reaches here, so this marks
        # every field line that does -- order is the contract: a
        # heartbeat must never set this.
        self._builder.saw_field_line = True
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "event":
            self._builder.event = value
        elif name == "data":
            self._accumulate(value)
        # Any other field name (id, retry, unrecognized) is ignored.
        return None

    def _accumulate(self, value: str) -> None:
        b = self._builder
        if b.oversized:
            return
        # Count the "\n" _dispatch() will insert before this part, so
        # the tracked total is exactly len("\n".join(parts)).
        b.char_count += len(value) + (1 if b.data_parts else 0)
        b.data_parts.append(value)
        if b.char_count > _MAX_FRAME_CHARS:
            b.oversized = True
            b.data_parts = []  # Stop holding the oversized content in memory.

    def _dispatch(self) -> _RawFrame | _Oversized | None:
        b = self._builder
        self._builder = _FrameBuilder()
        if b.oversized:
            return _OVERSIZED
        if not b.saw_field_line:
            return None  # A stray blank line: nothing preceded it.
        # A frame built only of field lines this module ignores (id:,
        # retry:, a field a later server adds) has no event name, so it
        # goes out as one -- drop reason #1, counted like every other
        # discarded frame, instead of vanishing.
        return _RawFrame(event=b.event, data="\n".join(b.data_parts))


def _reject_non_finite(constant: str) -> float:
    """``json.loads`` hook for ``NaN`` / ``Infinity`` / ``-Infinity``.

    Python's decoder accepts all three by default; JSON has no
    non-finite number literal, and the server's own serializer cannot
    produce one -- every payload it sends is either a pydantic
    ``model_dump(mode="json")`` (which renders a non-finite float as
    ``null``) or a hand-built literal. A frame carrying one is
    therefore malformed, and raising ``ValueError`` routes it into the
    same drop-and-count path as any other undecodable ``data:`` rather
    than delivering a float the wire contract never promised.
    """
    raise ValueError(f"{constant} is not valid JSON")


def _reject_non_finite_number(literal: str) -> float:
    """``json.loads`` hook for ordinary float literals.

    ``_reject_non_finite`` only fires for the three non-finite
    *tokens* (``NaN`` / ``Infinity`` / ``-Infinity``); an ordinary
    literal that overflows on conversion (``1e999``) is handled by the
    default ``float()`` and silently becomes ``inf`` unless this hook
    catches it too. Same conclusion either way: JSON has no non-finite
    number, so a frame carrying one is malformed and goes down the
    drop-and-count path. A literal that underflows to ``0.0`` (e.g.
    ``1e-999``) is a legitimate, representable JSON number and is not
    rejected here -- underflow does not produce a non-finite value.
    """
    value = float(literal)
    if not math.isfinite(value):
        raise ValueError(f"{literal} is not a finite JSON number")
    return value


def _parse_frame(raw: _RawFrame) -> StreamEvent | None:
    """Decode one assembled frame into a ``StreamEvent``, or ``None`` if
    it should be dropped.

    Five independent reasons drop a frame rather than raising: no
    ``event:`` line, an event name outside the 8 known ones (forward
    compatibility with a future 9th event), ``data:`` this decoder
    cannot parse (malformed, carrying a non-finite number literal or a
    float literal that overflows to one, or nested deeply enough to
    exhaust the recursion limit), JSON that decodes to something other
    than an object, and (for ``step.*`` frames only) a ``step`` payload
    that fails to parse as a ``Step`` -- most importantly a step type
    this SDK release does not know about, which is exactly the
    closed-enum fragility ``StepType`` documents. None of these may
    ever take down the whole stream: a live connection must survive
    one bad frame.

    An oversized *integer* literal is not a separate reason: CPython's
    string-to-int conversion already refuses one past
    ``sys.get_int_max_str_digits()`` with a ``ValueError``, which the
    ``except`` clause below catches like any other malformed ``data:``.
    There is no non-finite ``int``, so nothing else is needed there.

    One exception to the "data: does not parse -> drop" reason: a
    closing frame (``task.completed``, ``task.input_required``,
    ``stream.error``) whose body never arrived is delivered with an
    empty ``data``, not dropped -- its name alone already says the
    stream ended and how. A closing frame whose body arrived but does
    not decode is still dropped like any other frame; only "absent" is
    treated as observed fact, not "corrupt".
    """
    if raw.event is None or raw.event not in _KNOWN_EVENT_NAMES:
        return None
    if not raw.data and raw.event in _CLOSE_EVENT_NAMES:
        # A closing frame's meaning is carried by its name: it says the
        # stream ended, and how. A body that never arrived costs this
        # frame's payload, not the fact that the stream ended -- dropping
        # it would report a truncation at EOF for a stream the server
        # did close on purpose. A content frame is the reverse (its name
        # says nothing without its body), so this is deliberately not
        # extended to the other five names; and a body that did arrive
        # but does not decode is not synthesized either: "absent" is
        # something this module observed, "corrupt" would be a guess.
        return StreamEvent(event=raw.event, data={}, step=None)
    try:
        data = json.loads(
            raw.data,
            parse_constant=_reject_non_finite,
            parse_float=_reject_non_finite_number,
        )
    except (ValueError, RecursionError):
        # Two shapes of undecodable data land here. json.loads recurses
        # per nesting level, so a deeply nested `data:` payload
        # exhausts Python's recursion limit and raises RecursionError,
        # not ValueError. And `_reject_non_finite` /
        # `_reject_non_finite_number` raise ValueError for NaN /
        # Infinity / -Infinity and for a float literal that overflows
        # to one, which the decoder would otherwise accept or silently
        # coerce. Either way it is one bad frame, not a reason to take
        # down the stream.
        return None
    if not isinstance(data, dict):
        return None
    step: Step | None = None
    if raw.event in _STEP_EVENT_NAMES:
        try:
            step = _STEP_ADAPTER.validate_python(data.get("step"))
        except ValidationError:
            return None
    return StreamEvent(event=raw.event, data=data, step=step)


def _timeout_message(task_id: int, timeout: float | None, closed_by: str | None) -> str:
    """Shared wording for every ``TaskTimeout`` this module raises, so the
    proactive wall-clock checkpoints and the timeout classification
    branch (see ``_classify_timeout``) cannot drift apart.
    """
    return (
        f"Stream for task {task_id} did not close within {timeout}s "
        f"(last event: {closed_by})"
    )


def _classify_timeout(
    exc: httpx.TimeoutException,
    *,
    task_id: int,
    timeout: float | None,
    deadline: float | None,
    closed_by: str | None,
) -> TaskTimeout | XAgentTransportError:
    """Turn one ``httpx.TimeoutException`` into the right SDK exception.

    Every leg that can block -- connect, read, write, pool -- is
    clamped to the caller's own budget in ``open_task_event_stream()``,
    so any of them can be the way that budget runs out and all of them
    classify here. The same exception means two different things
    depending on the remaining wall-clock budget: budget exhausted
    means the caller's own ``timeout`` elapsed (``TaskTimeout``);
    budget still open means one leg hit its own ceiling with room to
    spare -- a connection that never came up, a pool slot that never
    freed, or a connection that stayed silent for a full read window
    without even a heartbeat -- which is a transport problem, not a
    deadline (``XAgentTransportError``). Shared by the open-time path
    (still waiting for response headers, or waiting for a slot) and
    every mid-stream timeout, so the two do not classify differently by
    accident. A read that times out inside the post-close drain does
    not reach this function at all -- see ``_draining()``.
    """
    if deadline is not None and time.monotonic() >= deadline:
        return TaskTimeout(
            "task_timeout",
            _timeout_message(task_id, timeout, closed_by),
            http_status=None,
        )
    return XAgentTransportError("transport_error", str(exc), http_status=None)


class TaskEventStream:
    """Iterator + context manager returned by ``AgentClient.tasks.events()``.

    See ``TasksAPI.events()`` for the full contract (event shapes,
    closing frames, timeout semantics, reconnection guidance). This
    class only implements the mechanics: read one line at a time from
    the open response, assemble frames, decode each into a
    ``StreamEvent``, and track how the stream ended.

    Not thread-safe: a single stream must be iterated and closed from
    the thread that opened it. The ``AgentClient`` that created it may
    still be shared across threads (its own guarantee is unaffected).

    The one exception is the ``__del__`` safety net: garbage collection
    runs on whatever thread happens to trigger it, so a stream
    abandoned without ``close()`` may have its connection released
    from a thread that never touched it. That is a best-effort
    backstop for a leak, not a second supported way to use a stream --
    the rule above still stands.
    """

    def __init__(
        self,
        *,
        connection: AbstractContextManager[tuple[httpx.Response, Iterator[str]]],
        lines: Iterator[str],
        task_id: int,
        deadline: float | None,
        timeout: float | None,
    ) -> None:
        self._connection = connection
        self._lines = lines
        self._task_id = task_id
        self._deadline = deadline
        self._timeout = timeout
        self._assembler = _FrameAssembler()
        self._closed_by: str | None = None
        self._last_event: StreamEvent | None = None
        self._dropped_frame_count = 0
        self._closed = False
        self._drain_deadline: float | None = None

    # --- Public read-only state -----------------------------------

    @property
    def task_id(self) -> int:
        return self._task_id

    @property
    def closed_by(self) -> str | None:
        """The ``event`` name of the last frame this stream delivered,
        or ``None`` if it delivered none. Set on every frame, not only
        closing frames -- pair it with "no exception escaped iteration"
        to tell a clean close from a stream that broke off mid-flight;
        see ``TasksAPI.events()`` for the full judgment call.
        """
        return self._closed_by

    @property
    def last_event(self) -> StreamEvent | None:
        """The last frame this stream delivered, or ``None`` if it
        delivered none. Preserved as-is across an exception raised out
        of iteration -- it is not cleared or overwritten by the failure.
        """
        return self._last_event

    @property
    def dropped_frame_count(self) -> int:
        """How many frames this stream discarded because they failed to
        parse, named an event this SDK release does not know about, or
        had no event name at all (a frame built only of field lines
        this module ignores, such as a bare ``id:``), or arrived after
        a closing frame, where the only frame this stream still accepts
        is the documented trailing ``stream.error`` (see
        ``_draining()``). Observability only -- there is no way to
        recover a dropped frame's content; ``TasksAPI.steps()`` is the
        only backstop.
        """
        return self._dropped_frame_count

    # --- Iterator protocol ------------------------------------------

    def __iter__(self) -> TaskEventStream:
        # Deliberately returns self, not a generator: see
        # TasksAPI.events() for why this is a contract, not an
        # implementation detail (re-iterating must not reopen the
        # connection).
        return self

    def __next__(self) -> StreamEvent:
        if self._closed:
            raise StopIteration
        try:
            return self._pull_next_event()
        except BaseException:
            # Single owner for "nothing leaves iteration with the
            # connection still open": covers a raising ``_next_line()``
            # leg (timeout or other httpx error), a truncated stream
            # reported by ``_finish_eof()``, and the frame-parsing step
            # itself, none of which close the connection on their own.
            # ``StopIteration`` on a clean EOF is caught here too --
            # it is a ``BaseException`` subclass like any other.
            self._close_quietly()
            raise

    def _pull_next_event(self) -> StreamEvent:
        while True:
            if self._draining():
                self._check_drain_window()
            else:
                self._check_deadline()
            line = self._next_line()
            if line is None:
                self._finish_eof()
                raise StopIteration
            parsed = self._assembler.feed(line)
            if isinstance(parsed, _RawFrame):
                event = _parse_frame(parsed)
                if event is None or (
                    self._draining() and not self._drain_accepts(event)
                ):
                    self._drop_frame()
                    continue
                return self._deliver(event)
            elif parsed is _OVERSIZED:
                self._drop_frame()

    def _draining(self) -> bool:
        """Whether this stream is in its bounded post-close drain.

        A closing frame ends the stream's content; what is left is the
        end of the response body and -- on an attach that could not take
        a complete step snapshot -- one trailing ``stream.error`` saying
        so (see the module docstring). Reading that tail out is the
        drain, and it is bounded twice over, because either bound alone
        leaves a peer or an intermediary able to hold the response, and
        its pool slot, open indefinitely:

        - by content: the frames a drain still delivers are the ones
          ``_drain_accepts()`` lists. Anything else ends it and is
          counted on ``dropped_frame_count``.
        - by time: ``_check_drain_window()`` gives the drain one read
          window, which is finite even when the caller set no budget.

        The caller's wall-clock budget deliberately does not govern this
        phase, and nothing in it raises ``TaskTimeout``: a task that
        finished on the last second of its budget is owed a clean close,
        so an elapsed window, a read that times out inside one, and a
        frame the drain does not accept all end the stream the way EOF
        does. Heartbeats are skipped here as they are anywhere else --
        they neither end the drain nor extend it, because the window is
        wall-clock and does not count frames.
        """
        return self._closed_by in _CLOSE_EVENT_NAMES

    def _check_drain_window(self) -> None:
        """Open the drain's window on first use, then enforce it.

        Counted from the moment the caller comes back for another event,
        not from the moment the closing frame was handed over. The time
        a caller spends holding that frame is its own; charging the
        drain for it would silently drop the trailing ``stream.error``
        -- the frame that says a step snapshot was incomplete -- for
        any caller that takes longer to process one event than the
        window lasts. What the window bounds is unaffected: a peer that
        will not stop sending is held to one window from the moment this
        stream starts reading again either way.

        Its length is the read window this stream already uses to wait
        for the next byte -- ``min(timeout, 60)``, and 60 seconds when
        there is no budget -- because the next byte is exactly what a
        drain is waiting for: one frame, or the end of the body.
        """
        if self._drain_deadline is None:
            self._drain_deadline = time.monotonic() + _clamped_leg(
                _STREAM_READ_TIMEOUT, self._timeout
            )
        if time.monotonic() >= self._drain_deadline:
            raise StopIteration

    def _drain_accepts(self, event: StreamEvent) -> bool:
        """Whether a drain still delivers ``event`` to the caller.

        One frame gets through: the trailing ``stream.error`` the server
        sends after a conclusion when an attach's step snapshot is
        incomplete (see the module docstring). Only one, because the
        conclusion it follows is never itself a ``stream.error`` -- so a
        second one is a peer repeating itself, not the documented tail.

        A frame that is not a closing frame at all is not a drain
        decision: the server does not produce that shape, and this
        module already reports the stream as truncated when the body
        ends (``_finish_eof()``). That judgment is deliberately left
        exactly as it was.
        """
        if event.event not in _CLOSE_EVENT_NAMES:
            return True
        return (
            event.event == _STREAM_ERROR_NAME and self._closed_by != _STREAM_ERROR_NAME
        )

    def _drop_frame(self) -> None:
        """Count one discarded frame, and end a drain on it.

        A drain accepts one frame and the end of the body; anything else
        on the wire is the peer continuing past the close, so the drain
        stops reading there rather than letting that traffic hold the
        response open. It is counted like every other discarded frame --
        a drain that threw frames away without saying so would break
        ``dropped_frame_count``'s own promise.
        """
        self._dropped_frame_count += 1
        if self._draining():
            raise StopIteration

    def _deliver(self, event: StreamEvent) -> StreamEvent:
        """Hand one frame to the caller, and record it as delivered.

        The single writer for ``last_event`` and ``closed_by``: both
        mean "the last frame this stream *delivered*", so a frame that
        is dropped, or one this module only looked at, must never set
        them. A frame that is not a closing frame also puts the caller's
        budget back in charge, so a window left over from an earlier
        closing frame is discarded here instead of being carried into a
        later one.
        """
        self._last_event = event
        self._closed_by = event.event
        if event.event not in _CLOSE_EVENT_NAMES:
            self._drain_deadline = None
        return event

    # --- Context manager ----------------------------------------------

    def __enter__(self) -> TaskEventStream:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        if exc_type is not None:
            # A close failure must not replace the exception already in
            # flight -- the caller is owed the error that describes what
            # happened, not an httpx teardown error.
            self._close_quietly()
        else:
            self.close()

    def __del__(self) -> None:
        # Delegates to the idempotent close() (via _close_quietly(), so
        # a failing release here is logged rather than lost), so
        # running after an explicit close() releases nothing a second
        # time. The outer suppress stays regardless: __del__ runs
        # during interpreter teardown on an unpredictable schedule, and
        # even the logging call itself can raise at that point. This is
        # the one path that can run on a thread other than the one that
        # opened the stream -- see the class docstring's thread-safety
        # note.
        with contextlib.suppress(Exception):
            self._close_quietly()

    def close(self) -> None:
        """Release the connection.

        Idempotent: the second and later calls do nothing. Also makes
        the stream stop iterating (further ``__next__`` calls raise
        ``StopIteration``) even if it was closed before reaching EOF --
        this is also what makes a second ``for`` loop over an
        already-exhausted stream return no frames instead of reopening
        the connection.

        A failing release is attempted exactly once, then reported.
        Retrying it is not possible, and would report success without
        doing anything:

        - ``self._connection`` is a ``@contextmanager`` generator. Its
          first ``__exit__`` already drove that generator past its final
          ``yield``; a second ``__exit__`` calls ``next()`` on an
          exhausted generator, receives ``StopIteration``, and returns
          ``False`` without running one line of release code.
        - The release underneath is ``httpx.Response.close()``, which
          sets ``is_closed = True`` *before* touching the stream, so
          even reached directly a second call returns immediately.
        - The connection lease is *not* guaranteed to go back to the
          pool. ``httpcore``'s ``PoolByteStream.close()`` marks itself
          closed, then closes the underlying stream -- where a raise
          can happen -- and only afterwards removes the request from
          the pool's queue; a raising close skips that removal, and
          because the closed flag is already set, nothing can retry it
          and get a second chance. The slot can stay held for the life
          of the client. So a failing release has to be reported
          rather than trusted to have cleaned up after itself -- see
          ``_close_quietly()``.

        So the stream is flagged closed either way, and a later
        ``close()``/``__del__`` is a no-op rather than a re-entry into a
        context manager's ``__exit__`` that already raised.

        ``_close_quietly()`` reports it on a best-effort basis: if the
        logging call itself raises -- a custom handler that fails --
        that failure is suppressed too, because a diagnostic must never
        take the place of the exception the caller is owed. A caller
        whose logging is broken loses this report; every other caller
        keeps it.
        """
        if self._closed:
            return
        try:
            self._connection.__exit__(None, None, None)
        finally:
            self._closed = True

    def _close_quietly(self) -> None:
        """Release on a failing path without letting a close failure
        replace the exception being raised: the caller is entitled to
        see the TaskTimeout / XAgentTransportError that actually
        describes what happened, not an httpx close error. Reported
        rather than swallowed -- a release that raises can leave this
        connection's pool slot held for the life of the client (see
        ``close()``), and nothing else would ever say so.
        """
        try:
            self.close()
        except Exception:
            with contextlib.suppress(Exception):
                logger.warning(
                    "releasing the event stream for task %s failed; its "
                    "connection may still be holding a slot in the client's "
                    "pool",
                    self._task_id,
                    exc_info=True,
                )

    # --- Internals ------------------------------------------------

    def _check_deadline(self) -> None:
        if self._deadline is not None and time.monotonic() >= self._deadline:
            self._close_quietly()
            raise TaskTimeout(
                "task_timeout",
                _timeout_message(self._task_id, self._timeout, self._closed_by),
                http_status=None,
            )

    def _next_line(self) -> str | None:
        """Pull the next line, or ``None`` when this stream has no more
        lines to take: the end of the response body, or a read that
        timed out inside the post-close drain (see ``_draining()``).

        Folds every lower-layer httpx failure into the SDK's exception
        hierarchy here, so the frame-assembly loop in
        ``_pull_next_event`` never sees a raw httpx exception. Each
        raising branch leaves the connection open on its own -- every
        failure here reaches ``__next__`` through ``_pull_next_event``,
        and ``__next__`` is the single owner that closes it on the way
        out (see its own comment).
        """
        try:
            return next(self._lines)
        except StopIteration:
            return None
        except httpx.TimeoutException as exc:
            if self._draining():
                # The stream already ended on a closing frame. A read
                # that times out while draining the rest of the body is
                # the drain reaching its own bound, not the caller's
                # budget elapsing, so it ends the stream the way EOF
                # does -- raising TaskTimeout here would time out a
                # stream that closed cleanly.
                return None
            raise _classify_timeout(
                exc,
                task_id=self._task_id,
                timeout=self._timeout,
                deadline=self._deadline,
                closed_by=self._closed_by,
            ) from exc
        except httpx.HTTPError as exc:
            raise XAgentTransportError(
                "transport_error", str(exc), http_status=None
            ) from exc

    def _finish_eof(self) -> None:
        """Called once ``_next_line()`` reports it has no more lines --
        the end of the response body, or a read that timed out while
        draining.

        The server does not raise an ``httpx.HTTPError`` on a clean
        close, so "EOF and the last frame delivered was not one of the
        three closing frames" is the only signal this module has for a
        truncated stream, and it has to synthesize the failure itself.
        An ordinary frame arriving after a closing one is not a shape
        the server produces (see the module docstring); if it happens
        anyway, this reports the connection as truncated where the body
        ended rather than passing it off as a clean close --
        deliberately strict. This branch is only ever reached from a
        genuine end of the body: a drain's own read timeout only
        happens once the last frame delivered already was a closing
        one, and this function does not raise in that case. Does not
        close the connection itself -- ``__next__`` does that on both
        the raising and the clean-``StopIteration`` path.
        """
        last = self._closed_by
        if last not in _CLOSE_EVENT_NAMES:
            detail = (
                "delivered no frames at all"
                if last is None
                else f"ended on {last!r}, which is not a closing frame"
            )
            raise XAgentTransportError(
                "transport_error",
                f"the task {self._task_id} event stream {detail}",
                http_status=None,
            )


def _is_utf8_charset(charset: str) -> bool:
    """Whether a declared ``charset`` names UTF-8.

    SSE bodies are UTF-8 by definition and this API hands the server's
    text back unchanged, so a response declaring anything else has to be
    refused rather than decoded: HTTPX would decode the body with the
    declared codec and replace whatever failed, producing a successful
    ``StreamEvent`` whose text is silently not what the server sent. An
    absent charset makes no such claim and is accepted -- the media type
    itself was already pinned above.

    Compared through ``codecs.lookup`` rather than by string match, so
    every registered spelling of the one acceptable codec (``utf-8``,
    ``UTF-8``, ``utf8``, ``u8``) passes and nothing else does --
    including ``us-ascii``, which would turn a byte the server sent as
    text into a decode error. The value comes from
    ``httpx.Response.charset_encoding``, the same header parse HTTPX
    decodes with, so a declaration accepted here is one HTTPX also
    decodes as UTF-8. A name no codec answers to is refused rather than
    left to HTTPX's fallback, which would quietly decode it as UTF-8
    anyway and hide the mislabeling.
    """
    try:
        return codecs.lookup(charset).name == "utf-8"
    except LookupError:
        return False


def open_task_event_stream(
    http: HTTPClient, task_id: int, *, timeout: float | None
) -> TaskEventStream:
    """Open the v1 task event stream. See ``TasksAPI.events()`` for the
    full public contract; this is its implementation.

    ``timeout`` must be a finite, non-negative number: a deadline built
    from ``NaN`` never compares true against ``time.monotonic()`` (a
    checkpoint that "elapses" would never actually fire), and one built
    from ``inf`` is equivalent to no budget at all but without saying
    so. Both are rejected here rather than accepted and silently
    behaving like ``None``.
    """
    if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
        raise ValueError("timeout must be a finite, non-negative number")
    deadline = None if timeout is None else time.monotonic() + timeout

    connection = http.stream_lines(
        "GET",
        f"/v1/chat/tasks/{task_id}/events",
        connect_timeout=_clamped_leg(_STREAM_CONNECT_TIMEOUT, timeout),
        read_timeout=_clamped_leg(_STREAM_READ_TIMEOUT, timeout),
        write_timeout=_clamped_leg(_STREAM_WRITE_TIMEOUT, timeout),
        pool_timeout=_clamped_leg(_STREAM_POOL_TIMEOUT, timeout),
    )
    try:
        resp, lines = connection.__enter__()
    except httpx.TimeoutException as exc:
        raise _classify_timeout(
            exc, task_id=task_id, timeout=timeout, deadline=deadline, closed_by=None
        ) from exc
    except httpx.HTTPError as exc:
        raise XAgentTransportError(
            "transport_error", str(exc), http_status=None
        ) from exc

    try:
        if resp.is_error:
            try:
                # Required before from_response(): see errors.from_response.
                resp.read()
            except httpx.TimeoutException as exc:
                raise _classify_timeout(
                    exc,
                    task_id=task_id,
                    timeout=timeout,
                    deadline=deadline,
                    closed_by=None,
                ) from exc
            except httpx.HTTPError as exc:
                raise XAgentTransportError(
                    "transport_error", str(exc), http_status=None
                ) from exc
            raise from_response(resp)

        if resp.status_code != 200:
            # 3xx (this client does not follow redirects) and 204 are
            # neither is_error nor the 200 a stream requires, so they
            # would otherwise reach the content-type branch and be
            # reported with no status at all.
            raise MalformedResponse(
                "malformed_response",
                f"Expected HTTP 200 for the task event stream, got {resp.status_code}",
                http_status=resp.status_code,
            )

        content_type = resp.headers.get("content-type", "")
        # Media types are case-insensitive (RFC 9110); the error text
        # below still echoes the original casing.
        if content_type.split(";", 1)[0].strip().lower() != "text/event-stream":
            raise MalformedResponse(
                "malformed_response",
                f"Expected content-type text/event-stream for the task "
                f"event stream, got {content_type!r}",
                http_status=None,
            )

        charset = resp.charset_encoding
        if charset is not None and not _is_utf8_charset(charset):
            raise MalformedResponse(
                "malformed_response",
                f"Expected a UTF-8 task event stream, got content-type "
                f"{content_type!r}",
                http_status=None,
            )
    except BaseException:
        # Covers Ctrl-C too: an interrupt during the error-body read must
        # not leak the connection either. A close failure here must not
        # replace the exception already in flight -- the caller is
        # owed the TaskNotFound / MalformedResponse that describes what
        # happened, not an httpx teardown error -- so it is reported
        # instead, the same as every other failing close in this module
        # (see ``TaskEventStream._close_quietly``).
        try:
            connection.__exit__(None, None, None)
        except Exception:
            with contextlib.suppress(Exception):
                logger.warning(
                    "releasing the event stream for task %s failed while "
                    "reporting an earlier open failure; its connection may "
                    "still be holding a slot in the client's pool",
                    task_id,
                    exc_info=True,
                )
        raise

    stream = TaskEventStream(
        connection=connection,
        lines=lines,
        task_id=task_id,
        deadline=deadline,
        timeout=timeout,
    )
    stream._check_deadline()  # Checkpoint (i): may close() + raise before any frame.
    return stream
