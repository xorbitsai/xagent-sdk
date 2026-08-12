import time
from typing import TYPE_CHECKING, Any

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

    All five endpoint methods are thin wrappers over the v1 endpoints:
    build a request body, hand it to ``AgentClient._request`` for
    transport + error mapping, then parse the success body into a frozen
    dataclass.

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
        ``WAITING_FOR_USER``.

        Raises:
            NoPendingInteraction: the task is not currently
                ``WAITING_FOR_USER`` (still running, only paused, or
                already terminal). Do not retry; use ``append()``
                instead.
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
