import time
from typing import TYPE_CHECKING, Any

from xagent_sdk._events import TaskEventStream, open_task_event_stream
from xagent_sdk.errors import TaskTimeout
from xagent_sdk.types import (
    AppendResult,
    CreateTaskResult,
    RunResult,
    Step,
    TaskInfo,
    TaskStatus,
    _parse_append,
    _parse_create_task,
    _parse_steps,
    _parse_task_info,
)

if TYPE_CHECKING:
    from xagent_sdk.agent_client import AgentClient


# Mirrors the backend's terminal set (``v1/tasks.py``): only COMPLETED and
# FAILED. PAUSED is *not* terminal -- the backend allows append() onto a
# PAUSED task (the atomic claim is ``WHERE status != RUNNING``), and
# ``completed_at`` is only populated in COMPLETED/FAILED. SDK stays
# consistent so multi-process workflows (A wait()s while B append()s a
# resume) observe the RUNNING transition rather than return early.
_TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED})

# States where wait() stops polling and hands the task back to the caller:
# a terminal state, or WAITING_FOR_USER. The task cannot advance out of
# WAITING_FOR_USER on its own -- it blocks on *this* caller answering its
# pending question via reply() -- so a passive poller would otherwise spin
# to timeout.
_WAIT_RETURN_STATUSES = _TERMINAL_STATUSES | frozenset({TaskStatus.WAITING_FOR_USER})


class TasksAPI:
    """The ``client.tasks`` namespace.

    Six of these methods map one-to-one onto a v1 endpoint. Five of them
    are thin wrappers: build a request body, hand it to
    ``AgentClient._request`` for transport + error mapping, then parse
    the success body into a frozen dataclass. ``events()`` is the
    exception -- it holds a streaming response open instead of parsing
    one body, so it does its own transport and error mapping (see its
    docstring).

    ``message`` arguments take a plain ``str`` rather than a structured
    object: the SDK only sends user-role messages (the v1 contract pins
    ``role="user"``), so the SDK wraps the string into
    ``{"role": "user", "content": ...}`` internally.

    ``agent_id`` is keyword-only on every write to prevent positional
    swaps with ``message``.

    ``wait()`` and ``run()`` add client-side polling on top of those
    endpoints; they raise ``TaskTimeout`` on deadline but propagate any
    other error from the underlying calls.
    """

    def __init__(self, client: "AgentClient") -> None:
        self._client = client

    def create(
        self,
        *,
        agent_id: int,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> CreateTaskResult:
        """``POST /v1/chat/tasks`` -- start a new task with the first user
        message. The server returns 202 with ``status='pending'``.
        """
        body: dict[str, Any] = {
            "agent_id": agent_id,
            "message": {"role": "user", "content": message},
        }
        if metadata is not None:
            body["metadata"] = metadata
        resp = self._client._request("POST", "/v1/chat/tasks", json=body)
        return _parse_create_task(resp.json())

    def append(
        self,
        task_id: int,
        *,
        agent_id: int,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> AppendResult:
        """``POST /v1/chat/tasks/{task_id}/messages`` -- append the next
        user turn to an existing task. Server returns 202 with
        ``status='running'`` (the atomic claim has already flipped the
        task row). Raises ``TaskBusy`` if the task is still running an
        earlier turn, or ``InteractionResponseRequired`` if the task is
        currently ``WAITING_FOR_USER`` -- answer its pending question
        with ``reply()`` instead.
        """
        body: dict[str, Any] = {
            "agent_id": agent_id,
            "message": {"role": "user", "content": message},
        }
        if metadata is not None:
            body["metadata"] = metadata
        resp = self._client._request(
            "POST", f"/v1/chat/tasks/{task_id}/messages", json=body
        )
        return _parse_append(resp.json())

    def reply(
        self,
        task_id: int,
        *,
        agent_id: int,
        message: str,
    ) -> AppendResult:
        """``POST /v1/chat/tasks/{task_id}/reply`` -- answer a task's
        pending question and resume its execution.

        This is a transitional, plain-text-only answer channel: it
        exists so a waiting task is unblockable at all. The typed
        ``respond`` surface (structured values matched to
        ``pending_interaction.interactions``, plus a real idempotency
        key) is tracked separately and will eventually replace this for
        callers that need it.

        Only valid while the task is ``WAITING_FOR_USER``. Inspect
        ``TaskInfo.pending_interaction`` (from ``get()`` or ``wait()``)
        for the question -- and any structured ``interactions`` -- before
        answering. Server returns 202 with ``status='running'``. Under
        the hood this resumes the same server-side run the task had
        before the reply, rather than starting a new one -- unlike
        ``append()`` -- but that continuity is server state, not
        something this call's ``AppendResult`` return value exposes;
        there is no ``run_id`` field on it to check.

        Not idempotent: unlike ``create()``/``append()``, there is no
        request-level idempotency key backing this call, so a
        client-side timeout does not tell you whether the answer was
        delivered -- retrying blindly can deliver it twice. If a call
        times out or the connection drops, call ``get(task_id)`` first;
        only retry ``reply()`` if ``status`` is still
        ``WAITING_FOR_USER``. Even that check is not a full guarantee:
        the first reply may have already been delivered and the agent
        may have immediately asked a *new* question, which also shows up
        as ``WAITING_FOR_USER``. If ``pending_interaction`` is not
        ``None``, comparing its ``question`` against the one you
        answered rules out most of these cases -- but not a follow-up
        question that happens to repeat the same text verbatim, and
        ``pending_interaction`` can itself be ``None`` (see
        ``PendingInteraction``), so "compare the question" is a partial
        mitigation, not a guarantee.

        Raises:
            NoPendingInteraction: the task is not currently
                ``WAITING_FOR_USER``: it is ``PENDING`` (has not started
                running yet), ``PAUSED``, or already terminal
                (``COMPLETED``/``FAILED``). Do not retry as-is. A
                ``PAUSED``, ``COMPLETED``, or ``FAILED`` task can take
                its next turn via ``append()``; a ``PENDING`` task
                cannot -- wait for it to start running first.
            InteractionNotResumable: the task's in-progress execution
                state could not be restored to accept the answer. The
                task stays ``WAITING_FOR_USER`` with no partial write;
                do not retry -- this specific pending question can no
                longer be answered, so start a new task instead.
            TemporarilyUnavailable: the execution state could not be
                read due to a transient failure. The task stays
                ``WAITING_FOR_USER``; safe to retry after a short
                backoff.
            TaskBusy: the task is currently ``RUNNING``, or another
                caller's reply/turn is already claiming this task.
                Retryable.
            TaskNotFound: unknown ``task_id``, or this runtime key does
                not own it.
            InternalError: falls back here for any response code the
                SDK does not recognize -- notably, calling ``reply()``
                against a server old enough to have no ``/reply`` route
                surfaces as this (FastAPI's 404 carries no V1 error
                envelope, so it cannot map to a more specific type).
                This happens whenever the SDK is upgraded ahead of the
                server it talks to.
        """
        body: dict[str, Any] = {
            "agent_id": agent_id,
            "message": {"role": "user", "content": message},
        }
        resp = self._client._request(
            "POST", f"/v1/chat/tasks/{task_id}/reply", json=body
        )
        return _parse_append(resp.json())

    def get(self, task_id: int) -> TaskInfo:
        """``GET /v1/chat/tasks/{task_id}`` -- snapshot the current task
        row. ``output`` is populated once status reaches ``completed``.
        """
        resp = self._client._request("GET", f"/v1/chat/tasks/{task_id}")
        return _parse_task_info(resp.json())

    def steps(self, task_id: int) -> list[Step]:
        """``GET /v1/chat/tasks/{task_id}/steps`` -- full timeline so far
        in ``started_at`` ascending order. In-flight steps appear with
        ``status='running'`` so the caller can poll and observe progress.
        """
        resp = self._client._request("GET", f"/v1/chat/tasks/{task_id}/steps")
        return _parse_steps(resp.json())

    def events(self, task_id: int, *, timeout: float | None = None) -> TaskEventStream:
        """``GET /v1/chat/tasks/{task_id}/events`` -- attach to the task's
        live event stream instead of polling ``get()`` / ``steps()``.

        Returns a ``TaskEventStream``: an iterator of ``StreamEvent``
        (``for event in stream: ...``) that is also a context manager
        (``with client.tasks.events(task_id) as stream: ...``, which
        guarantees the connection closes on exit). Iterating it a second
        time after it has ended yields nothing -- it does not reopen the
        connection.

        Delivers 8 possible ``event`` names: ``task.status``,
        ``step.started``, ``step.completed``, ``message.delta``,
        ``message.completed``, and the three *closing* frames --
        ``task.completed``, ``task.input_required``, ``stream.error`` --
        after which the server ends the connection. ``event.step`` is a
        ``Step`` (the same dataclass ``steps()`` returns) on
        ``step.*`` frames and ``None`` otherwise; every other field
        stays in ``event.data`` exactly as the server sent it (one
        exception: a body-less closing frame, described below), including
        ``status`` on ``task.status`` / ``task.completed`` -- it is kept
        as a plain string, not ``TaskStatus``, so a status value this
        SDK release does not know about still reaches you. A frame
        naming an event this SDK release does not know about, or one
        that otherwise fails to parse (malformed JSON, an unknown
        ``step.type``), is dropped rather than raised: count these on
        ``stream.dropped_frame_count`` and treat them as observability,
        not as something to recover -- ``steps()`` is the only complete,
        untruncated record.

        Per-event field reference for ``event.data`` (describing current
        server behavior: the server can add a field to any of these
        without a corresponding SDK release, and this reference does not
        update itself when that happens -- an unrecognized key still
        reaches you unchanged, per the "no key is renamed, added, or
        removed" rule above):

          - ``task.status`` -- ``{"status": str}``. Always exactly this
            one key.
          - ``step.started`` / ``step.completed`` -- ``{"step": {...}}``,
            the same object ``event.step`` is parsed from (``Step.id`` /
            ``type`` / ``status`` / ``started_at`` / ``completed_at`` /
            ``data``; see ``Step``'s own docstring for its type-specific
            ``data`` keys). The ``"step"`` key is not removed from
            ``event.data`` once ``event.step`` is populated -- they are
            the same information read two different ways.
          - ``message.delta`` -- ``{"message_id": str, "text": str}``,
            plus ``"truncated": True`` only when the server capped this
            chunk's length; omitted (not ``False``) when it did not.
          - ``message.completed`` -- ``{"message_id": str, "content":
            str}``, with the same optional ``"truncated": True`` as
            ``message.delta``.
          - ``task.completed`` (closing) -- ``{"status": str, "output":
            str | None, "error": str | None}``, plus
            ``"snapshot_truncated": True`` and ``"snapshot_total_steps":
            int`` together, only on the two attach-time fast-path exits
            where the step snapshot handed to this connection was cut
            short by a size cap -- absent (not ``null``) otherwise.
          - ``task.input_required`` (closing) -- ``{"task_id": int,
            "prompt": str | None}``, plus the same optional
            ``snapshot_truncated`` / ``snapshot_total_steps`` pair as
            ``task.completed``, for the same reason.
          - ``stream.error`` (closing) -- ``{"code": str, "message":
            str}``, both always present. ``code`` is a short
            machine-readable string meant to be branched on; the
            server's current set is ``"resync_required"``,
            ``"unauthorized"``, ``"task_deleted"``, ``"stream_expired"``
            -- treat it as open-ended, not exhaustive.

        Any of the three closing events above can instead deliver
        ``event.data == {}`` when its body never reached this connection
        (see the closing-frame contract below) -- the field lists above
        describe a body that did arrive.

        Once the loop ends -- normally or via an exception -- check
        ``stream.closed_by`` (the ``event`` name of the last frame
        delivered, or ``None`` if none arrived) and ``stream.last_event``
        (that frame itself). An exception leaves both exactly as they
        were at the last frame received, so a caller catching
        ``XAgentTransportError``/``TaskTimeout`` can still see how far
        the stream got. A clean end of the loop with ``closed_by`` in
        ``{"task.completed", "task.input_required", "stream.error"}`` is
        the only shape that means "the server closed this on purpose" --
        note ``stream.error`` is a *normal* close in this contract, not
        a raised exception, because the server still uses it for
        ordinary reasons such as its 1-hour per-stream cap. A closing
        frame whose body never reached this connection still closes the
        stream this way, delivered with ``event.data == {}`` -- its
        name alone already says how the stream ended, so read its
        fields with ``.get()`` rather than assuming ``"code"`` or
        ``"status"`` is always present. Anything else reaching EOF
        (including zero frames at all) raises
        ``XAgentTransportError`` itself, because the server ends a
        stream this way with no ``httpx`` exception to detect it by.
        Content frames are best-effort: the server can silently drop one
        (queue overflow, an oversized inbound frame, a step that never
        gets a matching completion) with no signal on the wire, so
        "content missing from this stream" never proves it did not
        happen -- only ``steps()`` is authoritative.

        A step's ``id`` is comparable against the same id from
        ``steps()`` only for ``tool_call``, ``agent_delegation``, and
        ``thinking`` steps whose id embeds a source step/tool-call id
        (see ``Step.id``); ``message`` steps and planning steps
        (``thinking:plan:...`` / ``thinking:planning:...``) are not --
        reconcile a planning step by ``started_at`` plus content, never
        by id, and never merge one you saw on the stream with one you
        already have from ``steps()`` just because the ids match.

        A final answer can reach this stream twice: once as a
        ``message.delta`` sequence plus ``message.completed``, and again
        as a ``message`` step's ``step.completed`` -- the server does
        this on purpose rather than risk the two channels disagreeing
        about whether that step exists. The recommended way to fold
        this back into one string: accumulate
        by ``message_id`` across ``message.delta``/``message.completed``
        (preferring the accumulated text over a ``truncated``
        ``message.completed``), and drop a ``message`` step's
        ``step.completed`` only when its ``data["role"] == "assistant"``
        *and* this connection has already delivered at least one
        ``message.*`` frame -- never drop one with any other
        ``data["role"]`` (e.g. ``"user"``), it is not a duplicate.

        Args:
            task_id: The task to attach to.
            timeout: Wall-clock budget in seconds for the whole call,
                including the time spent opening the connection and the
                time this call is idle between frames -- once you get an
                event back, the clock keeps running while your code
                holds onto it before asking for the next one. Must be a
                finite, non-negative number. It is a budget enforced at
                checkpoints, not a hard cap -- see the overrun note at
                the end.
                ``None`` (the default) sets no local budget -- the
                connection still cannot idle past the server's
                15-second heartbeat, or its 1-hour per-stream cap.
                ``0`` is legal but not instantaneous: it still opens
                the connection once (so a 401/404/429 still maps the
                same way), and only raises ``TaskTimeout`` -- before
                delivering any event -- once that finishes. With no
                budget to narrow them, each phase gets its own full
                ceiling there, so the worst case is the same
                one-after-another chain described below at each
                phase's ceiling.

                It is a soft budget, not a hard cap. Every phase that
                can block is clamped to it individually -- connect, the
                wait for a free connection, and writing the request
                each get ``min(timeout, 10)`` seconds, each read window
                gets ``min(timeout, 60)`` -- but those phases run one
                after another, so a call that is unlucky in several of
                them in a row can outlast the number you passed. The
                worst case runs the pool wait, then the connect, then
                the request write, then each read window, and finally
                one post-close drain window (below), one after
                another. So a budget spent stuck connecting, stuck
                waiting for a connection, stuck writing, or stuck
                waiting for the next byte raises ``TaskTimeout``; the
                reverse case -- a leg hitting its own ceiling while the
                budget still has room -- is ``XAgentTransportError``.
                ``AgentClient(timeout=...)`` has no effect here: this
                call always overrides it with its own request-level
                timeout. The budget is checked between reads, never
                during one, so a read already in flight when the
                deadline passes runs to its own window's end first.
                That window cannot be narrowed as the deadline
                approaches: the underlying HTTP layer fixes the read
                timeout when the response body starts being read.

                Once a closing frame (``task.completed``,
                ``task.input_required``, or ``stream.error``) has
                arrived, this budget stops applying -- a task that
                finished on the last second of it is owed a clean close
                -- and hands over to a bounded post-close drain. The
                drain reads out what is left of the response: the end
                of the body, and, on an attach that could not take a
                complete step snapshot, one trailing ``stream.error``
                saying so. It accepts nothing else: a repeated closing
                frame, a frame that does not decode, or one past the
                size cap ends the stream and counts on
                ``dropped_frame_count``. It also gets one read window
                of its own, counted from the moment you ask for the
                next event rather than from the moment the closing
                frame reached you, so time you spend processing that
                frame is not charged to it. Nothing in the drain raises
                ``TaskTimeout``: an elapsed window and a read that
                times out inside one both end the stream the way the
                end of the body does, and heartbeats are skipped as
                they are anywhere else. An ordinary frame after a
                closing one is not part of the drain -- the server does
                not produce that shape; it is still delivered, and the
                stream is still reported as truncated when the body
                ends.

        Returns:
            A ``TaskEventStream`` bound to this ``task_id``.

        Raises:
            ValueError: ``timeout`` is negative, ``NaN``, or infinite.
            InvalidAPIKey: the API key is missing, invalid, or revoked.
            TaskNotFound: unknown ``task_id``, or this runtime key does
                not own it.
            RateLimited: this task already has 2 open streams, or this
                key has 32 open streams across all of its tasks -- both
                caps are enforced on the server's side. Back off before
                retrying.
            MalformedResponse: the response was 200 but its
                content-type was not ``text/event-stream`` (e.g. a proxy
                returned an HTML error page), or the response's status
                was neither an error nor 200 (a redirect or a 204) --
                that status is carried on ``http_status`` in this case,
                or the response declared a charset other than UTF-8 (an
                absent charset is fine).
            XAgentTransportError: a network failure, or the connection's
                last frame was not a closing frame.
            TaskTimeout: the ``timeout`` budget elapsed first.
            InternalError: falls back here for any response the SDK
                does not recognize -- notably, calling ``events()``
                against a server old enough to have no ``/events`` route
                surfaces as this with ``http_status=404`` (FastAPI's 404
                carries no V1 error envelope), distinguishable from a
                real ``TaskNotFound`` by exception type. This happens
                whenever the SDK is upgraded ahead of the server it
                talks to.

        A task already ``paused`` does not close this stream on its
        own -- pass a ``timeout`` (or rely on the server's 1-hour cap)
        if you attach to one; otherwise the call blocks until some
        other caller's ``append()`` resumes it.

        This call does not retry or reconnect on its own: attach again
        with a fresh ``events(task_id)`` after resolving what closed the
        previous one, reconciling against ``steps()`` first because the
        server never replays what happened before this connection
        opened. Do the reconciliation in the order that avoids a gap
        between the two sources: attach *before* calling ``steps()``,
        not after, so nothing that happens in between is missed by
        both.

        Not thread-safe: a single stream must be iterated and closed
        from the thread that opened it. The ``AgentClient`` that created
        it stays shareable across threads -- that guarantee is
        unaffected. The only exception is garbage collection releasing
        an abandoned stream's connection as a best-effort backstop,
        which can run on any thread -- that is not a second supported
        way to use a stream.
        """
        return open_task_event_stream(self._client._http, task_id, timeout=timeout)

    def wait(
        self,
        task_id: int,
        *,
        timeout: float = 120.0,
        poll_interval: float = 1.0,
    ) -> TaskInfo:
        """Poll ``get()`` until the task stops needing the SDK to wait.

        Returns as soon as the task reaches a terminal state
        (``COMPLETED``/``FAILED``) or ``WAITING_FOR_USER``. The latter is
        not terminal but blocks on this caller answering the task's
        pending question, so ``wait()`` hands it back: inspect
        ``TaskInfo.pending_interaction`` for the question, then
        ``reply()`` the user's answer and ``wait()`` again. ``PENDING``,
        ``RUNNING``, and ``PAUSED`` keep the loop going; a PAUSED task can
        be resumed by an ``append()`` from another caller, and waiting
        through it lets one observer see the resulting RUNNING
        transition.

        Returns the ``TaskInfo`` once one of those states is observed.
        Raises ``TaskTimeout`` if the wall-clock deadline elapses first.
        Any other exception raised by ``get()`` (``XAgentTransportError``,
        ``TaskNotFound``, ``InvalidAPIKey``, ...) propagates immediately
        -- this helper deliberately does not retry transient failures,
        because retry semantics belong to the caller's business logic.

        Args:
            task_id: The task to poll.
            timeout: Maximum wall-clock seconds to wait. Must be
                non-negative; ``0`` polls exactly once and raises
                ``TaskTimeout`` immediately if the task has not reached a
                returnable state. Default 120.
            poll_interval: Seconds to sleep between polls. Must be
                non-negative; ``0`` tight-loops (yields the GIL each
                iteration via ``time.sleep(0)``). Default 1.0.

        Returns:
            The ``TaskInfo`` snapshot at the returnable state.

        Raises:
            ValueError: ``timeout`` or ``poll_interval`` is negative.
            TaskTimeout: ``timeout`` elapsed without a returnable state.
        """
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        if poll_interval < 0:
            raise ValueError("poll_interval must be non-negative")
        deadline = time.monotonic() + timeout
        while True:
            info = self.get(task_id)
            if info.status in _WAIT_RETURN_STATUSES:
                return info
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TaskTimeout(
                    "task_timeout",
                    (
                        f"Task {task_id} did not reach a returnable state "
                        f"within {timeout}s "
                        f"(last observed status: {info.status.value})"
                    ),
                    http_status=None,
                )
            # Cap the sleep so a long ``poll_interval`` cannot overshoot
            # the caller's requested wall-clock timeout.
            time.sleep(min(poll_interval, remaining))

    def run(
        self,
        *,
        agent_id: int,
        message: str,
        timeout: float = 120.0,
        poll_interval: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> RunResult:
        """Single-turn convenience: ``create()`` + ``wait()`` + ``steps()``.

        Equivalent to::

            created = client.tasks.create(agent_id=..., message=...)
            info    = client.tasks.wait(created.task_id, timeout=...)
            steps   = client.tasks.steps(created.task_id)

        ``timeout`` is the wall-clock budget for ``create`` + ``wait``
        combined: the time spent in ``create()`` is subtracted from the
        budget passed to ``wait()`` so the caller does not pay it twice.
        ``steps()`` is invoked once ``wait()`` returns and is a single
        cheap GET; its latency is additional but expected to be a small
        constant.

        The returned ``RunResult`` is terminal (``COMPLETED``/``FAILED``)
        unless the agent asked a question: a ``WAITING_FOR_USER`` status
        means the task is blocked on an answer -- inspect
        ``result.info.pending_interaction`` for the question, answer it
        with ``reply()``, and ``wait()`` again -- or use the lower-level
        trio directly when you need multiple turns or to interleave other
        work.

        Raises ``TaskTimeout`` if the task does not reach a returnable
        state within the combined ``create`` + ``wait`` budget. Other
        errors propagate from the underlying ``create`` / ``get`` /
        ``steps`` calls.
        """
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        if poll_interval < 0:
            raise ValueError("poll_interval must be non-negative")
        start = time.monotonic()
        created = self.create(agent_id=agent_id, message=message, metadata=metadata)
        remaining = max(0.0, timeout - (time.monotonic() - start))
        info = self.wait(
            created.task_id, timeout=remaining, poll_interval=poll_interval
        )
        steps = self.steps(created.task_id)
        return RunResult(info=info, steps=steps)
