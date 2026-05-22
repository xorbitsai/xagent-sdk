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
    from xagent_sdk.client import XAgentClient


# Mirrors backend ``v1/tasks.py:170``: ``_TERMINAL_STATUSES = (COMPLETED,
# FAILED)``. PAUSED is *not* terminal -- backend allows append() onto a
# PAUSED task (the atomic claim is ``WHERE status != RUNNING``), and
# ``completed_at`` is only populated in COMPLETED/FAILED. SDK stays
# consistent so multi-process workflows (A wait()s while B append()s a
# resume) observe the RUNNING transition rather than return early.
_TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED})


class TasksAPI:
    """The ``client.tasks`` namespace.

    All four endpoint methods are thin wrappers over the v1 endpoints:
    build a request body, hand it to ``XAgentClient._request`` for
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

    def __init__(self, client: "XAgentClient") -> None:
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
        earlier turn.
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
        """Poll ``get()`` until the task reaches a terminal state.

        Terminal states are ``COMPLETED`` and ``FAILED`` -- mirroring the
        backend's own definition. ``PENDING``, ``RUNNING``, and ``PAUSED``
        keep the loop going; a PAUSED task can be resumed by an
        ``append()`` from another caller, and waiting through it lets one
        observer see the resulting RUNNING transition.

        Returns the final ``TaskInfo`` once a terminal state is observed.
        Raises ``TaskTimeout`` if the wall-clock deadline elapses first.
        Any other exception raised by ``get()`` (``XAgentTransportError``,
        ``TaskNotFound``, ``InvalidAPIKey``, ...) propagates immediately
        -- this helper deliberately does not retry transient failures,
        because retry semantics belong to the caller's business logic.

        Args:
            task_id: The task to poll.
            timeout: Maximum wall-clock seconds to wait. Must be
                non-negative; ``0`` polls exactly once and raises
                ``TaskTimeout`` immediately if the task is still
                non-terminal. Default 120.
            poll_interval: Seconds to sleep between polls. Must be
                non-negative; ``0`` tight-loops (yields the GIL each
                iteration via ``time.sleep(0)``). Default 1.0.

        Returns:
            The final ``TaskInfo`` snapshot.

        Raises:
            ValueError: ``timeout`` or ``poll_interval`` is negative.
            TaskTimeout: ``timeout`` elapsed without a terminal state.
        """
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        if poll_interval < 0:
            raise ValueError("poll_interval must be non-negative")
        deadline = time.monotonic() + timeout
        while True:
            info = self.get(task_id)
            if info.status in _TERMINAL_STATUSES:
                return info
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TaskTimeout(
                    "task_timeout",
                    (
                        f"Task {task_id} did not reach a terminal state "
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
        ``steps()`` is invoked after a terminal state is observed and is
        a single cheap GET; its latency is additional but expected to be
        a small constant.

        Use the lower-level trio when you need to send multiple turns or
        interleave other work.

        Raises ``TaskTimeout`` if the task does not terminate within the
        combined ``create`` + ``wait`` budget. Other errors propagate
        from the underlying ``create`` / ``get`` / ``steps`` calls.
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
