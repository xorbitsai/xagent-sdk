"""Tests for dataclass models and pydantic parsers."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from xagent_sdk import (
    AppendResult,
    CreateTaskResult,
    RunResult,
    Step,
    StepType,
    TaskInfo,
    TaskStatus,
)
from xagent_sdk.types import (
    _parse_append,
    _parse_create_task,
    _parse_steps,
    _parse_task_info,
)


class TestParseCreateTask:
    def test_pending_status_and_tz(self) -> None:
        c = _parse_create_task(
            {
                "task_id": 10,
                "agent_id": 7,
                "status": "pending",
                "created_at": "2026-05-10T03:00:00Z",
            }
        )
        assert c.status is TaskStatus.PENDING
        assert c.created_at.tzinfo is not None
        assert isinstance(c, CreateTaskResult)


class TestParseAppend:
    def test_accepted_at_field(self) -> None:
        a = _parse_append(
            {
                "task_id": 10,
                "agent_id": 7,
                "status": "running",
                "accepted_at": "2026-05-10T03:01:00Z",
            }
        )
        assert a.status is TaskStatus.RUNNING
        assert isinstance(a.accepted_at, datetime)
        assert isinstance(a, AppendResult)


class TestParseTaskInfo:
    def test_running_with_nulls(self) -> None:
        t = _parse_task_info(
            {
                "task_id": 10,
                "agent_id": 7,
                "status": "running",
                "input": "hi",
                "output": None,
                "error": None,
                "created_at": "2026-05-10T03:00:00Z",
                "completed_at": None,
            }
        )
        assert t.output is None
        assert t.completed_at is None

    def test_completed_with_values(self) -> None:
        t = _parse_task_info(
            {
                "task_id": 10,
                "agent_id": 7,
                "status": "completed",
                "input": "hi",
                "output": "hello",
                "error": None,
                "created_at": "2026-05-10T03:00:00Z",
                "completed_at": "2026-05-10T03:00:05Z",
            }
        )
        assert t.status is TaskStatus.COMPLETED
        assert t.output == "hello"
        assert isinstance(t, TaskInfo)

    def test_unknown_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _parse_task_info(
                {
                    "task_id": 1,
                    "agent_id": 1,
                    "status": "cosmic_ray",
                    "input": None,
                    "output": None,
                    "error": None,
                    "created_at": "2026-05-10T03:00:00Z",
                    "completed_at": None,
                }
            )


class TestParseSteps:
    @pytest.mark.parametrize(
        ("type_str", "enum_val"),
        [
            ("message", StepType.MESSAGE),
            ("thinking", StepType.THINKING),
            ("tool_call", StepType.TOOL_CALL),
            ("agent_delegation", StepType.AGENT_DELEGATION),
        ],
    )
    def test_all_four_step_types(self, type_str: str, enum_val: StepType) -> None:
        payload = {
            "task_id": 10,
            "agent_id": 7,
            "steps": [
                {
                    "id": f"{type_str}:1",
                    "type": type_str,
                    "status": "completed",
                    "started_at": "2026-05-10T03:00:00Z",
                    "completed_at": "2026-05-10T03:00:00Z",
                    "data": {"k": "v"},
                }
            ],
        }
        steps = _parse_steps(payload)
        assert len(steps) == 1
        assert steps[0].type is enum_val
        assert isinstance(steps[0], Step)

    def test_data_preserved(self) -> None:
        payload = {
            "steps": [
                {
                    "id": "tool_call:1",
                    "type": "tool_call",
                    "status": "running",
                    "started_at": "2026-05-10T03:00:00Z",
                    "completed_at": None,
                    "data": {
                        "name": "execute_python_code",
                        "args": {"code": "1+1"},
                    },
                }
            ]
        }
        steps = _parse_steps(payload)
        assert steps[0].data["name"] == "execute_python_code"
        assert steps[0].data["args"]["code"] == "1+1"

    def test_empty_list(self) -> None:
        assert _parse_steps({"task_id": 1, "agent_id": 1, "steps": []}) == []

    @pytest.mark.parametrize(
        "data",
        [None, [1, 2, 3], "garbage", 42, True],
    )
    def test_non_dict_returns_empty(self, data: object) -> None:
        # Defensive: malformed response (server bug or upstream proxy
        # rewriting the body) should not crash with AttributeError.
        assert _parse_steps(data) == []

    def test_unknown_step_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _parse_steps(
                {
                    "steps": [
                        {
                            "id": "foo:1",
                            "type": "comet_strike",
                            "status": "completed",
                            "started_at": "2026-05-10T03:00:00Z",
                            "completed_at": "2026-05-10T03:00:00Z",
                            "data": {},
                        }
                    ]
                }
            )


class TestFrozenDataclasses:
    def test_step_frozen(self) -> None:
        step = Step(
            id="message:1",
            type=StepType.MESSAGE,
            status="completed",
            started_at=datetime(2026, 5, 10, tzinfo=UTC),
            completed_at=datetime(2026, 5, 10, tzinfo=UTC),
            data={"role": "user", "content": "hi"},
        )
        with pytest.raises(FrozenInstanceError):
            step.id = "hacked"  # type: ignore[misc]


class TestRunResult:
    def _info(self, status: TaskStatus, output: str | None) -> TaskInfo:
        return TaskInfo(
            task_id=1,
            agent_id=1,
            status=status,
            input="hi",
            output=output,
            error=None,
            created_at=datetime(2026, 5, 10, tzinfo=UTC),
            completed_at=datetime(2026, 5, 10, 0, 0, 1, tzinfo=UTC),
        )

    def test_output_shortcut(self) -> None:
        r = RunResult(info=self._info(TaskStatus.COMPLETED, "hello"), steps=[])
        assert r.output == "hello"

    def test_status_shortcut(self) -> None:
        r = RunResult(info=self._info(TaskStatus.FAILED, None), steps=[])
        assert r.status is TaskStatus.FAILED
