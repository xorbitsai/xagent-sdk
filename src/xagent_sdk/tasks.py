from typing import TYPE_CHECKING, Any

from xagent_sdk.types import (
    AppendResult,
    CreateTaskResult,
    Step,
    TaskInfo,
    _parse_append,
    _parse_create_task,
    _parse_steps,
    _parse_task_info,
)

if TYPE_CHECKING:
    from xagent_sdk.client import XAgentClient


class TasksAPI:
    """The ``client.tasks`` namespace.

    All four methods are thin wrappers over the v1 endpoints: build a
    request body, hand it to ``XAgentClient._request`` for transport +
    error mapping, then parse the success body into a frozen dataclass.

    ``message`` arguments take a plain ``str`` rather than a structured
    object: the SDK only sends user-role messages (the v1 contract pins
    ``role="user"``), so the SDK wraps the string into
    ``{"role": "user", "content": ...}`` internally.

    ``agent_id`` is keyword-only on every write to prevent positional
    swaps with ``message``.
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
