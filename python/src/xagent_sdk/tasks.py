import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from xagent_sdk._base import _json_body
from xagent_sdk._events import TaskEventStream, open_task_event_stream
from xagent_sdk._structured import expected_echo, prepare_response_format
from xagent_sdk.errors import (
    TaskTimeout,
    UnsupportedFeature,
    XAgentError,
    _run_mismatch,
)
from xagent_sdk.types import (
    AppendResult,
    CreateTaskResult,
    RunResult,
    Step,
    TaskInfo,
    TaskStatus,
    _parse_append,
    _parse_create_task,
    _parse_features,
    _parse_steps,
    _parse_task_info,
)

if TYPE_CHECKING:
    from xagent_sdk._base import _BaseClient


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

# The name ``GET /v1/features`` lists when the key can set a response_format.
_STRUCTURED_OUTPUT_FEATURE = "structured_output"


class TasksAPI:
    """The ``client.tasks`` namespace, on both ``AgentClient`` and ``UserClient``.

    Six of these methods map one-to-one onto a v1 endpoint. Five of them
    build a request body, hand it to the owning client's ``_request`` for
    transport + error mapping, then parse the success body into a frozen
    dataclass. With a ``response_format``, ``create()`` and ``append()`` also
    check it locally, ask ``GET /v1/features`` (once per namespace) whether
    the server supports it, and check the format the server echoes back.
    ``events()`` is the exception -- it holds a streaming response open
    instead of parsing one body, so it does its own transport and error
    mapping (see its docstring).

    ``message`` arguments take a plain ``str`` rather than a structured
    object: the SDK only sends user-role messages (the v1 contract pins
    ``role="user"``), so the SDK wraps the string into
    ``{"role": "user", "content": ...}`` internally.

    ``agent_id`` is keyword-only on every write to prevent positional
    swaps with ``message``.

    ``wait()`` and ``run()`` add client-side polling on top of those
    endpoints; they raise ``TaskTimeout`` on deadline but propagate any
    other error from the underlying calls.

    With an agent key (``AgentClient.tasks``), ``agent_id`` must be the
    key's own agent. With a personal key (``UserClient.tasks``), ``agent_id``
    may be any agent the key holder can see, and the task runs as the key
    holder rather than as the agent's owner. Which tasks a personal key can
    see, and when a task can be continued, is described on ``UserClient``.
    """

    def __init__(self, client: "_BaseClient") -> None:
        self._client = client
        # Whether the server lets this key use structured output, as last
        # read from ``GET /v1/features``; ``None`` until a call needs it.
        self._structured_output_supported: bool | None = None

    def _require_structured_output(self) -> None:
        """Raise ``UnsupportedFeature`` unless this server lets the key use
        structured output, asking ``GET /v1/features`` the first time.

        A 200 answer and a 404 (no such route) are remembered on this
        namespace; any other failure is raised unchanged and not remembered,
        so the next call asks again.
        """
        if self._structured_output_supported is None:
            try:
                resp = self._client._request("GET", "/v1/features")
            except XAgentError as exc:
                if exc.http_status != 404:
                    raise
                self._structured_output_supported = False
            else:
                features = _parse_features(_json_body(resp, "features"))
                self._structured_output_supported = (
                    _STRUCTURED_OUTPUT_FEATURE in features
                )
        if not self._structured_output_supported:
            raise UnsupportedFeature(
                "unsupported_feature",
                "This server or key does not support structured output "
                "(GET /v1/features does not list structured_output); "
                "nothing was sent.",
            )

    def create(
        self,
        *,
        agent_id: int,
        message: str,
        metadata: dict[str, Any] | None = None,
        dispatch_class: Literal["interactive", "batch"] | None = None,
        priority: int | None = None,
        prompt_context: Mapping[str, str | int | float | bool] | None = None,
        response_format: dict[str, Any] | type[BaseModel] | None = None,
    ) -> CreateTaskResult:
        """``POST /v1/chat/tasks`` -- start a new task with the first user
        message. The server returns 202 with ``status='pending'`` when
        it queues the task for its dispatcher, or ``status='running'``
        when it runs the task in-process.

        ``dispatch_class`` (``'interactive'`` or ``'batch'``) and
        ``priority`` (an integer from -10 to 10, where an omitted value
        means 0) are sent only when not ``None``; the server owns their
        validation and answers 422 (``InvalidInput``) for anything it
        rejects. They apply to this new task only: ``append()`` and
        ``reply()`` do not accept them, the task keeps the values given
        here, and its later execution commands inherit the priority
        unless the deployment's own classification sets one.
        Whether the server honors ``dispatch_class`` is a deployment
        setting -- read ``CreateTaskResult.dispatch_class`` to see the
        class the start command was given. A server that predates these
        fields ignores them silently and reports ``dispatch_class=None``.

        ``prompt_context`` is reference data the server stores on the task and
        shows the agent on every turn, as data rather than as instructions. It
        is sent only when not ``None``; ``{}`` is sent as given and leaves
        nothing stored. The SDK does not check its keys, value types or size:
        the server owns those rules and measures the size on its own rendering
        of the value. Anything it rejects raises ``InvalidInput`` (HTTP 422) with
        the reason in ``message``, which is free text to show as it is, not to
        parse. A ``nan`` or ``inf`` value raises ``ValueError`` before the
        task request is sent with httpx 0.28 or later; older httpx versions send it
        and the server answers ``InvalidInput``. A value JSON cannot encode,
        such as a ``date``, raises ``TypeError`` before the task request is sent.
        A server that predates this field ignores it silently.

        How the server treats the values depends on the key. With an agent key
        they count as signed by your backend, and the agent's declared fields do
        not limit them. With a personal key they count as unsigned, with the
        same trust as user input: every field must be one the agent declares
        (its ``prompt_context_fields``) and be no longer than the declared
        length, otherwise the whole request is rejected, nothing is stored, and
        ``message`` names the rejected fields. This SDK can neither set nor read
        an agent's declared fields. Declare them on the agent in the web agent
        builder.

        ``response_format`` asks the server for a structured result that
        follows a JSON Schema, read from ``TaskInfo.structured_output`` once
        the task completes (``TaskInfo.parsed()`` does this). Pass a JSON
        object (a ``dict``) such as
        ``{"type": "json_schema", "json_schema": {"name": ..., "schema": ...}}``
        (``json_schema`` may also carry ``description`` and ``strict: true``),
        ``{"type": "text"}`` for none, or a Pydantic model class, which is
        converted into the supported schema subset and named after the class
        (see "Structured output" in the README); then pass the same class to
        ``parsed()``. It is sent only when not ``None``, and unlike the fields
        above it is never silently ignored:

          1. The value is written out and read back as the JSON that will be
             sent, and checked against the server's rules that do not depend
             on its size. A value that breaks one, or a model the subset
             cannot express, raises ``UnsupportedSchema`` before any request.
             A value JSON cannot encode raises ``TypeError`` or
             ``ValueError`` before any request when writing the value out
             fails. A NaN, an infinity or a lone surrogate in a position the
             local check does not cover raises ``ValueError`` after step 2,
             when the request body is encoded, with httpx 0.28 or later;
             older httpx versions send it and the server answers 422
             (``InvalidInput``). The server checks the rest and answers 422
             (``InvalidInput``).
          2. Once per ``tasks`` namespace, ``GET /v1/features`` is asked
             whether this server and key support structured output. A 404 (a
             server that predates it) or a list without ``structured_output``
             (a key that cannot set a format) raises ``UnsupportedFeature``
             before the task is created; the answer is remembered. Any other
             failure of that call (a 5xx, a timeout, a 401) is raised as it is
             and asked again on the next call.
          3. The response must echo the format back
             (``response_format_type`` and ``response_format_name``). If it
             does not, the task was created but the format may not be in
             force, and ``UnsupportedFeature`` is raised with that
             ``task_id``.
        """
        sent_format: Any = None
        if response_format is not None:
            sent_format = prepare_response_format(response_format)
            self._require_structured_output()
        body: dict[str, Any] = {
            "agent_id": agent_id,
            "message": {"role": "user", "content": message},
        }
        if metadata is not None:
            body["metadata"] = metadata
        if dispatch_class is not None:
            body["dispatch_class"] = dispatch_class
        if priority is not None:
            body["priority"] = priority
        if prompt_context is not None:
            body["prompt_context"] = dict(prompt_context)
        if response_format is not None:
            body["response_format"] = sent_format
        resp = self._client._request("POST", "/v1/chat/tasks", json=body)
        raw = _json_body(resp, "task creation", 202)
        created = _parse_create_task(raw)
        if response_format is not None:
            _check_echo(raw, sent_format, created.task_id)
        return created

    def append(
        self,
        task_id: int,
        *,
        agent_id: int,
        message: str,
        metadata: dict[str, Any] | None = None,
        prompt_context: Mapping[str, str | int | float | bool] | None = None,
        response_format: dict[str, Any] | type[BaseModel] | None = None,
    ) -> AppendResult:
        """``POST /v1/chat/tasks/{task_id}/messages`` -- append the next
        user turn to an existing task. Server returns 202 with
        ``status='pending'`` when it queues the turn for its dispatcher,
        or ``status='running'`` when it runs the turn in-process. Raises
        ``TaskBusy`` if the task is still running an earlier turn, or
        ``InteractionResponseRequired`` if the task is currently
        ``WAITING_FOR_USER`` -- answer its pending question with
        ``reply()`` instead.

        ``prompt_context`` follows the rules described on ``create()``. Here
        ``None`` (the default) sends nothing and the task keeps what it has
        stored; an object replaces the stored value for this and later turns;
        ``{}`` clears it, so later turns carry none.

        ``response_format`` is a JSON object (a ``dict``) or a Pydantic model
        class, checked and sent as described on ``create()``.
        ``None`` (the default) sends nothing and the task keeps its format; a
        ``json_schema`` value replaces it for this and later turns;
        ``{"type": "text"}`` removes it. An ``UnsupportedFeature`` raised
        after the server accepted the turn carries this ``task_id``.
        """
        sent_format: Any = None
        if response_format is not None:
            sent_format = prepare_response_format(response_format)
            self._require_structured_output()
        body: dict[str, Any] = {
            "agent_id": agent_id,
            "message": {"role": "user", "content": message},
        }
        if metadata is not None:
            body["metadata"] = metadata
        if prompt_context is not None:
            body["prompt_context"] = dict(prompt_context)
        if response_format is not None:
            body["response_format"] = sent_format
        resp = self._client._request(
            "POST", f"/v1/chat/tasks/{task_id}/messages", json=body
        )
        raw = _json_body(resp, "task message", 202)
        appended = _parse_append(raw)
        if response_format is not None:
            _check_echo(raw, sent_format, appended.task_id)
        return appended

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
        ``append()`` -- so the ``run_id`` on the returned
        ``AppendResult`` is the run the task already had, not a new one.

        Not idempotent: this SDK sends no request-level idempotency key
        with this call, so a client-side timeout does not tell you
        whether the answer was delivered -- retrying blindly can deliver
        it twice. If a call times out or the connection drops, call
        ``get(task_id)`` first; only retry ``reply()`` if ``status`` is
        still ``WAITING_FOR_USER``. That retry can raise ``TaskBusy``
        while the server is still handling an earlier answer; it does
        not mean your answer was applied (the earlier answer, which may
        be another caller's, can still be rejected); call ``get()``
        again later and repeat the check. Even that check is not a
        full guarantee: the first reply may have already been
        delivered and the agent may have immediately asked a *new*
        question, which also shows up as ``WAITING_FOR_USER``. If
        ``pending_interaction`` is not ``None``, comparing its
        ``question`` against the one you answered rules out most of
        these cases -- but not a follow-up question that happens to
        repeat the same text verbatim, and ``pending_interaction`` can
        itself be ``None`` (see ``PendingInteraction``), so "compare
        the question" is a partial mitigation, not a guarantee.

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
            TaskNotFound: unknown ``task_id``, or this key cannot see
                it.
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
        return _parse_append(_json_body(resp, "task reply", 202))

    def get(self, task_id: int) -> TaskInfo:
        """``GET /v1/chat/tasks/{task_id}`` -- snapshot the current task
        row. ``output`` is populated once status reaches ``completed``.
        """
        resp = self._client._request("GET", f"/v1/chat/tasks/{task_id}")
        return _parse_task_info(_json_body(resp, "task"))

    def steps(self, task_id: int) -> list[Step]:
        """``GET /v1/chat/tasks/{task_id}/steps`` -- full timeline so far
        in ``started_at`` ascending order. In-flight steps appear with
        ``status='running'`` so the caller can poll and observe progress.
        """
        resp = self._client._request("GET", f"/v1/chat/tasks/{task_id}/steps")
        return _parse_steps(_json_body(resp, "task steps"))

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
            str | None, "error": str | None, "structured_output": dict |
            None, "structured_output_status": str | None,
            "structured_output_reason": dict | None}`` -- the three
            structured fields carry the same values as the fields of the
            same name on ``get()`` (the reason as the raw object, not a
            ``StructuredOutputReason``), and a server that predates them
            leaves them out -- plus
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
                The client's ``timeout=`` setting has no effect here: this
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
            TaskNotFound: unknown ``task_id``, or this key cannot see
                it.
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
        from the thread that opened it. The client that created
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
        expected_run_id: str | None = None,
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

        With ``expected_run_id`` (the ``run_id`` of the ``create()`` or
        ``append()`` you are waiting for), every snapshot ``wait()`` is
        about to return -- terminal or ``WAITING_FOR_USER`` -- must show
        that run; one that shows another run, or no run (``run_id`` is
        ``None``), raises ``RunMismatch`` (code ``run_mismatch``) instead
        of being returned, and polling stops. So a question asked by a
        turn another caller appended is never handed to this caller;
        ``RunMismatch`` lists the other causes. Snapshots that keep the
        loop going are not compared. Without ``expected_run_id`` no
        snapshot is compared. If a turn appended to a ``PAUSED`` task is
        refused before it starts, the task stays ``PAUSED`` and ``wait()``
        runs until ``TaskTimeout``.
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
            expected_run_id: The run the returned snapshot must show, or
                ``None`` (the default) to accept any run.

        Returns:
            The ``TaskInfo`` snapshot at the returnable state.

        Raises:
            ValueError: ``timeout`` or ``poll_interval`` is negative.
            TaskTimeout: ``timeout`` elapsed without a returnable state.
            RunMismatch: ``expected_run_id`` was given and the returnable
                snapshot shows another run or no run (code
                ``run_mismatch``).
        """
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        if poll_interval < 0:
            raise ValueError("poll_interval must be non-negative")
        deadline = time.monotonic() + timeout
        while True:
            info = self.get(task_id)
            if info.status in _WAIT_RETURN_STATUSES:
                if expected_run_id is not None and info.run_id != expected_run_id:
                    raise _run_mismatch(task_id, expected_run_id, info.run_id)
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
        dispatch_class: Literal["interactive", "batch"] | None = None,
        priority: int | None = None,
        prompt_context: Mapping[str, str | int | float | bool] | None = None,
        response_format: dict[str, Any] | type[BaseModel] | None = None,
    ) -> RunResult:
        """Single-turn convenience: ``create()`` + ``wait()`` + ``steps()``.

        Equivalent to::

            created = client.tasks.create(agent_id=..., message=...)
            info    = client.tasks.wait(
                created.task_id, timeout=..., expected_run_id=created.run_id
            )
            steps   = client.tasks.steps(created.task_id)

        ``metadata``, ``dispatch_class``, ``priority``, ``prompt_context`` and
        ``response_format`` are passed to ``create()`` unchanged; see its
        docstring. With a ``response_format``, read the result with
        ``RunResult.parsed()``.

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

        The returned ``RunResult.run_id`` is ``created.run_id``, and
        ``wait()`` has checked that ``info`` shows that run: a snapshot of
        another run, or of no run, raises ``RunMismatch`` (code
        ``run_mismatch``), whose ``task_id`` is the task this call created.
        A server that does not report a ``run_id`` on ``create()`` leaves it
        ``None`` and nothing is compared.

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
        created = self.create(
            agent_id=agent_id,
            message=message,
            metadata=metadata,
            dispatch_class=dispatch_class,
            priority=priority,
            prompt_context=prompt_context,
            response_format=response_format,
        )
        remaining = max(0.0, timeout - (time.monotonic() - start))
        info = self.wait(
            created.task_id,
            timeout=remaining,
            poll_interval=poll_interval,
            expected_run_id=created.run_id,
        )
        steps = self.steps(created.task_id)
        return RunResult(info=info, steps=steps, run_id=created.run_id)


def _check_echo(raw: Any, sent_format: Any, task_id: int) -> None:
    """Raise ``UnsupportedFeature`` for ``task_id`` unless the 202 body ``raw``
    echoes the format that was sent.

    ``raw`` is the body that has just been parsed into a result, so it is a
    dict. A server that ignores ``response_format`` sends neither echo key.
    """
    expected = expected_echo(sent_format)
    echoed = (
        raw.get("response_format_type", None),
        raw.get("response_format_name", None),
    )
    if (
        "response_format_type" not in raw
        or "response_format_name" not in raw
        or echoed != expected
    ):
        raise UnsupportedFeature(
            "unsupported_feature",
            f"The server accepted the request for task {task_id} but did not "
            "confirm the response_format that was sent (expected "
            f"response_format_type={expected[0]!r} and "
            f"response_format_name={expected[1]!r}); the task may not have "
            "this format.",
            task_id=task_id,
        )
