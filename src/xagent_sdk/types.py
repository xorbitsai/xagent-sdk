from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import TypeAdapter


class TaskStatus(str, Enum):
    """Lifecycle states a task can hold.

    The full set the SDK may observe is fixed at 5 values; ``run()`` and
    ``wait()`` treat ``COMPLETED``, ``FAILED``, and ``PAUSED`` as terminal.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


class StepType(str, Enum):
    """Public timeline step types.

    Backend collapses ~32 internal trace event types into these 4 stable
    surface values. New step types may be added in future server versions;
    callers should treat unknown types as forward-compat extensions.
    """

    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    AGENT_DELEGATION = "agent_delegation"
    MESSAGE = "message"


@dataclass(frozen=True)
class MeResponse:
    """``GET /v1/me`` payload -- agent identity bound to the presented key."""

    agent_id: int
    agent_name: str
    key_prefix: str


@dataclass(frozen=True)
class CreateTaskResult:
    """``POST /v1/chat/tasks`` 202 payload.

    ``status`` is always ``TaskStatus.PENDING`` at this point; observe
    transitions via ``client.tasks.get()`` or ``client.tasks.wait()``.
    """

    task_id: int
    agent_id: int
    status: TaskStatus
    created_at: datetime


@dataclass(frozen=True)
class AppendResult:
    """``POST /v1/chat/tasks/{id}/messages`` 202 payload.

    ``status`` is ``TaskStatus.RUNNING`` -- the backend's atomic claim has
    already flipped the task row by the time the response is built. The
    timestamp field is ``accepted_at`` (when the server scheduled
    execution), not ``created_at``.
    """

    task_id: int
    agent_id: int
    status: TaskStatus
    accepted_at: datetime


@dataclass(frozen=True)
class TaskInfo:
    """``GET /v1/chat/tasks/{id}`` payload -- snapshot of one task row.

    ``input`` / ``output`` reflect the latest turn only; full transcript
    history is reconstructed from ``client.tasks.steps()`` by filtering
    ``step.type == StepType.MESSAGE``.
    """

    task_id: int
    agent_id: int
    status: TaskStatus
    input: str | None
    output: str | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True)
class Step:
    """One step on the public agent timeline.

    Type-specific ``data`` keys (kept as untyped ``dict[str, Any]`` because
    tool args / results carry arbitrary JSON):

      - ``StepType.THINKING``         -- ``{"phase": "planning" | "step" | "action"}``
      - ``StepType.TOOL_CALL``        -- ``{"name": str, "args": Any,
                                            "result"?: Any, "error"?: str}``
      - ``StepType.AGENT_DELEGATION`` -- ``{"sub_agent_name": str,
                                            "input"?: Any, "output"?: Any}``
      - ``StepType.MESSAGE``          -- ``{"role": "user" | "assistant",
                                            "content": str}``

    ``id`` is a string with a type prefix (e.g. ``"tool_call:abc123"``) and
    is stable across re-polls, so callers can de-duplicate while streaming.
    """

    id: str
    type: StepType
    status: str  # "running" | "completed" | "failed"
    started_at: datetime
    completed_at: datetime | None
    data: dict[str, Any]


@dataclass(frozen=True)
class RunResult:
    """Bundled result of ``client.tasks.run()``.

    Carries the final ``TaskInfo`` snapshot together with the full step
    timeline so callers do not need to re-fetch either after a
    convenience run. The ``status`` and ``output`` properties shortcut
    the most common reads (``result.output`` vs ``result.info.output``).
    """

    info: TaskInfo
    steps: list[Step]

    @property
    def output(self) -> str | None:
        return self.info.output

    @property
    def status(self) -> TaskStatus:
        return self.info.status


# --- Private parsers --------------------------------------------------
# TypeAdapter caches schema per type at module import; cheap to keep at
# module scope. ``validate_python`` handles ISO datetime parsing, enum
# coercion, and Optional handling without bespoke conversion code.

_ME_ADAPTER = TypeAdapter(MeResponse)
_CREATE_ADAPTER = TypeAdapter(CreateTaskResult)
_APPEND_ADAPTER = TypeAdapter(AppendResult)
_TASK_INFO_ADAPTER = TypeAdapter(TaskInfo)
_STEP_LIST_ADAPTER = TypeAdapter(list[Step])


def _parse_me(data: dict[str, Any]) -> MeResponse:
    return _ME_ADAPTER.validate_python(data)


def _parse_create_task(data: dict[str, Any]) -> CreateTaskResult:
    return _CREATE_ADAPTER.validate_python(data)


def _parse_append(data: dict[str, Any]) -> AppendResult:
    return _APPEND_ADAPTER.validate_python(data)


def _parse_task_info(data: dict[str, Any]) -> TaskInfo:
    return _TASK_INFO_ADAPTER.validate_python(data)


def _parse_steps(data: dict[str, Any]) -> list[Step]:
    """Extract and parse the ``steps`` array from a ``StepsResponse`` body.

    The backend wraps the array in a top-level object with ``task_id`` and
    ``agent_id`` repeated for self-description; the SDK caller invoked
    ``client.tasks.steps(task_id)`` so those wrapper fields are redundant
    and dropped here.
    """
    return _STEP_LIST_ADAPTER.validate_python(data.get("steps", []))
