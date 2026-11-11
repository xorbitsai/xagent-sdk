"""The run a result belongs to: ``run_id`` on every task result, and the
``expected_run_id`` comparison of ``wait()`` and ``run()``."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from xagent_sdk import (
    AgentClient,
    RunMismatch,
    RunResult,
    TaskInfo,
    TaskStatus,
    TaskTimeout,
)
from xagent_sdk.types import _parse_append, _parse_create_task, _parse_task_info

from ._fixtures import response


def _task(status: str, run_id: str | None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "task_id": 10,
        "agent_id": 7,
        "status": status,
        "input": "hi",
        "output": "done" if status == "completed" else None,
        "error": None,
        "created_at": "2026-05-10T03:00:00Z",
        "completed_at": (
            "2026-05-10T03:00:05Z" if status in ("completed", "failed") else None
        ),
        **extra,
    }
    if run_id is not None:
        body["run_id"] = run_id
    return body


def _polling_handler(
    snapshots: list[dict[str, Any]], calls: list[str]
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve ``snapshots`` in order to ``GET /v1/chat/tasks/10``, then keep
    serving the last one."""

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(f"{req.method} {req.url.path}")
        index = min(sum(c.startswith("GET") for c in calls) - 1, len(snapshots) - 1)
        return httpx.Response(200, json=snapshots[index])

    return handler


class TestRunIdParsed:
    def test_write_results_carry_run_id(self) -> None:
        created = _parse_create_task(response("create_task_dispatch_class"))
        appended = _parse_append({**response("append_task"), "run_id": "run_2"})
        assert created.run_id == "run_0a1b2c3d"
        assert appended.run_id == "run_2"

    def test_older_server_shapes_default_to_none(self) -> None:
        assert _parse_create_task(response("create_task")).run_id is None
        assert _parse_append(response("append_task")).run_id is None
        assert _parse_task_info(response("task_info_completed")).run_id is None

    def test_task_info_carries_run_id(self) -> None:
        info = _parse_task_info({**response("task_info_completed"), "run_id": "r"})
        assert info.run_id == "r"


class TestReplyRunId:
    def test_reply_returns_the_run_it_resumes(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path == "/v1/chat/tasks/10/reply"
            return httpx.Response(
                202, json={**response("reply_task"), "run_id": "run_1"}
            )

        with make_client(handler) as c:
            assert c.tasks.reply(10, agent_id=7, message="yes").run_id == "run_1"


class TestWaitExpectedRunId:
    @pytest.mark.parametrize("status", ["completed", "failed", "waiting_for_user"])
    def test_mismatch_on_returnable_snapshot_raises_and_stops(
        self, make_client: Callable[..., AgentClient], status: str
    ) -> None:
        calls: list[str] = []
        extra = (
            {"pending_interaction": {"question": "Which?", "interactions": None}}
            if status == "waiting_for_user"
            else {}
        )
        handler = _polling_handler([_task(status, "run_other", **extra)], calls)
        with make_client(handler) as c, pytest.raises(RunMismatch) as exc:
            c.tasks.wait(10, timeout=2.0, poll_interval=0.0, expected_run_id="run_1")
        assert exc.value.code == "run_mismatch"
        assert exc.value.http_status is None
        assert exc.value.task_id == 10
        assert exc.value.expected_run_id == "run_1"
        assert exc.value.actual_run_id == "run_other"
        assert "Task 10" in exc.value.message
        assert calls == ["GET /v1/chat/tasks/10"]

    def test_snapshot_without_run_id_raises(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # A new task whose start was refused reads FAILED with no run.
        calls: list[str] = []
        handler = _polling_handler([_task("failed", None)], calls)
        with make_client(handler) as c, pytest.raises(RunMismatch) as exc:
            c.tasks.wait(10, timeout=2.0, poll_interval=0.0, expected_run_id="run_1")
        assert exc.value.actual_run_id is None
        assert "no run" in exc.value.message
        assert calls == ["GET /v1/chat/tasks/10"]

    def test_matching_run_is_returned(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        calls: list[str] = []
        handler = _polling_handler([_task("completed", "run_1")], calls)
        with make_client(handler) as c:
            info = c.tasks.wait(10, timeout=2.0, expected_run_id="run_1")
        assert info.status is TaskStatus.COMPLETED
        assert info.run_id == "run_1"

    @pytest.mark.parametrize("status", ["pending", "running", "paused"])
    def test_snapshots_that_keep_polling_are_not_compared(
        self, make_client: Callable[..., AgentClient], status: str
    ) -> None:
        calls: list[str] = []
        snapshots = [_task(status, "run_old"), _task("completed", "run_1")]
        handler = _polling_handler(snapshots, calls)
        with make_client(handler) as c:
            info = c.tasks.wait(
                10, timeout=2.0, poll_interval=0.0, expected_run_id="run_1"
            )
        assert info.run_id == "run_1"
        assert len(calls) == 2

    @pytest.mark.parametrize("run_id", ["run_other", None])
    def test_without_expected_run_id_any_run_is_returned(
        self, make_client: Callable[..., AgentClient], run_id: str | None
    ) -> None:
        calls: list[str] = []
        handler = _polling_handler([_task("completed", run_id)], calls)
        with make_client(handler) as c:
            info = c.tasks.wait(10, timeout=2.0)
        assert info.status is TaskStatus.COMPLETED
        assert info.run_id == run_id

    def test_paused_row_that_never_changes_times_out(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # A turn refused before it starts leaves the task PAUSED on the old
        # run; wait() keeps polling it and ends with TaskTimeout.
        calls: list[str] = []
        handler = _polling_handler([_task("paused", "run_old")], calls)
        with make_client(handler) as c, pytest.raises(TaskTimeout):
            c.tasks.wait(10, timeout=0.05, poll_interval=0.01, expected_run_id="r")


def _run_handler(
    create_run_id: str | None, info_run_id: str | None, calls: list[str]
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(f"{req.method} {req.url.path}")
        if req.method == "POST":
            body: dict[str, Any] = {**response("create_task"), "task_id": 10}
            if create_run_id is not None:
                body["run_id"] = create_run_id
            return httpx.Response(202, json=body)
        if req.url.path.endswith("/steps"):
            return httpx.Response(200, json={"task_id": 10, "steps": []})
        return httpx.Response(200, json=_task("completed", info_run_id))

    return handler


class TestRunRunId:
    def test_run_result_carries_the_created_run(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        calls: list[str] = []
        with make_client(_run_handler("run_1", "run_1", calls)) as c:
            result = c.tasks.run(agent_id=7, message="hi", poll_interval=0.0)
        assert result.run_id == "run_1"
        assert result.info.run_id == "run_1"

    def test_run_raises_when_the_snapshot_shows_another_run(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        calls: list[str] = []
        with (
            make_client(_run_handler("run_1", "run_2", calls)) as c,
            pytest.raises(RunMismatch) as exc,
        ):
            c.tasks.run(agent_id=7, message="hi", poll_interval=0.0)
        assert exc.value.code == "run_mismatch"
        assert exc.value.task_id == 10
        assert calls == ["POST /v1/chat/tasks", "GET /v1/chat/tasks/10"]

    def test_run_raises_when_the_snapshot_shows_no_run(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        calls: list[str] = []
        with (
            make_client(_run_handler("run_1", None, calls)) as c,
            pytest.raises(RunMismatch) as exc,
        ):
            c.tasks.run(agent_id=7, message="hi", poll_interval=0.0)
        assert exc.value.task_id == 10
        assert exc.value.actual_run_id is None
        assert calls == ["POST /v1/chat/tasks", "GET /v1/chat/tasks/10"]

    def test_run_on_a_server_without_run_id_compares_nothing(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        calls: list[str] = []
        with make_client(_run_handler(None, "run_2", calls)) as c:
            result = c.tasks.run(agent_id=7, message="hi", poll_interval=0.0)
        assert result.run_id is None
        assert result.info.run_id == "run_2"


class TestRunResultConstruction:
    def _info(self) -> TaskInfo:
        return TaskInfo(
            task_id=1,
            agent_id=1,
            status=TaskStatus.COMPLETED,
            input="hi",
            output="ok",
            error=None,
            created_at=datetime(2026, 5, 10, tzinfo=UTC),
            completed_at=datetime(2026, 5, 10, tzinfo=UTC),
        )

    def test_positional_and_keyword_construction_default_run_id(self) -> None:
        info = self._info()
        for result in (RunResult(info, []), RunResult(info=info, steps=[])):
            assert result.run_id is None
            assert result.info is info
