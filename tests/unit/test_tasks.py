"""Tests for TasksAPI: 5 endpoint methods plus wait + run helpers."""

import json
import time
from collections.abc import Callable

import httpx
import pytest

from xagent_sdk import (
    AgentNotFound,
    AppendResult,
    CreateTaskResult,
    InvalidInput,
    RunResult,
    StepType,
    TaskBusy,
    TaskInfo,
    TaskNotFound,
    TaskStatus,
    TaskTimeout,
    XAgentClient,
)


class TestCreate:
    def test_body_shape(self, make_client: Callable[..., XAgentClient]) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(
                202,
                json={
                    "task_id": 10,
                    "agent_id": 7,
                    "status": "pending",
                    "created_at": "2026-05-10T03:00:00Z",
                },
            )

        with make_client(handler) as c:
            result = c.tasks.create(agent_id=7, message="hi")

        assert captured[0].method == "POST"
        assert captured[0].url.path == "/v1/chat/tasks"
        body = json.loads(captured[0].content)
        assert body == {
            "agent_id": 7,
            "message": {"role": "user", "content": "hi"},
        }
        assert isinstance(result, CreateTaskResult)
        assert result.status is TaskStatus.PENDING

    def test_metadata_included(self, make_client: Callable[..., XAgentClient]) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(
                202,
                json={
                    "task_id": 10,
                    "agent_id": 7,
                    "status": "pending",
                    "created_at": "2026-05-10T03:00:00Z",
                },
            )

        with make_client(handler) as c:
            c.tasks.create(agent_id=7, message="hi", metadata={"trace_id": "abc"})

        body = json.loads(captured[0].content)
        assert body["metadata"] == {"trace_id": "abc"}

    def test_no_metadata_field_when_none(
        self, make_client: Callable[..., XAgentClient]
    ) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(
                202,
                json={
                    "task_id": 10,
                    "agent_id": 7,
                    "status": "pending",
                    "created_at": "2026-05-10T03:00:00Z",
                },
            )

        with make_client(handler) as c:
            c.tasks.create(agent_id=7, message="hi")

        body = json.loads(captured[0].content)
        assert "metadata" not in body


class TestAppend:
    def test_url_and_body(self, make_client: Callable[..., XAgentClient]) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(
                202,
                json={
                    "task_id": 10,
                    "agent_id": 7,
                    "status": "running",
                    "accepted_at": "2026-05-10T03:01:00Z",
                },
            )

        with make_client(handler) as c:
            result = c.tasks.append(10, agent_id=7, message="next")

        assert captured[0].method == "POST"
        assert captured[0].url.path == "/v1/chat/tasks/10/messages"
        body = json.loads(captured[0].content)
        assert body == {
            "agent_id": 7,
            "message": {"role": "user", "content": "next"},
        }
        assert isinstance(result, AppendResult)
        assert result.status is TaskStatus.RUNNING


class TestGet:
    def test_url_and_parse(self, make_client: Callable[..., XAgentClient]) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(
                200,
                json={
                    "task_id": 10,
                    "agent_id": 7,
                    "status": "completed",
                    "input": "hi",
                    "output": "hello",
                    "error": None,
                    "created_at": "2026-05-10T03:00:00Z",
                    "completed_at": "2026-05-10T03:00:05Z",
                },
            )

        with make_client(handler) as c:
            info = c.tasks.get(10)

        assert captured[0].method == "GET"
        assert captured[0].url.path == "/v1/chat/tasks/10"
        assert isinstance(info, TaskInfo)
        assert info.output == "hello"


class TestSteps:
    def test_url_and_outer_wrapper_dropped(
        self, make_client: Callable[..., XAgentClient]
    ) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(
                200,
                json={
                    "task_id": 10,
                    "agent_id": 7,
                    "steps": [
                        {
                            "id": "message:1",
                            "type": "message",
                            "status": "completed",
                            "started_at": "2026-05-10T03:00:00Z",
                            "completed_at": "2026-05-10T03:00:00Z",
                            "data": {"role": "assistant", "content": "hi"},
                        }
                    ],
                },
            )

        with make_client(handler) as c:
            steps = c.tasks.steps(10)

        assert captured[0].method == "GET"
        assert captured[0].url.path == "/v1/chat/tasks/10/steps"
        assert len(steps) == 1
        assert steps[0].type is StepType.MESSAGE


class TestErrorMappingPerEndpoint:
    """Each endpoint shares the _request error mapping; one error per
    endpoint confirms the wiring."""

    @pytest.mark.parametrize(
        ("status", "code", "exc_cls", "call"),
        [
            (
                404,
                "agent_not_found",
                AgentNotFound,
                lambda c: c.tasks.create(agent_id=1, message="x"),
            ),
            (
                409,
                "task_busy",
                TaskBusy,
                lambda c: c.tasks.append(10, agent_id=1, message="x"),
            ),
            (
                404,
                "task_not_found",
                TaskNotFound,
                lambda c: c.tasks.get(10),
            ),
        ],
    )
    def test_envelope_codes(
        self,
        make_client: Callable[..., XAgentClient],
        status: int,
        code: str,
        exc_cls: type[Exception],
        call: Callable[[XAgentClient], object],
    ) -> None:
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status, json={"error": {"code": code, "message": "m"}}
            )

        with make_client(h) as c, pytest.raises(exc_cls) as excinfo:
            call(c)
        # Each XAgentError subclass carries the server code; mypy can't
        # know exc_cls is a subclass here, so cast via attribute access.
        assert excinfo.value.code == code  # type: ignore[attr-defined]

    def test_steps_422(self, make_client: Callable[..., XAgentClient]) -> None:
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json={"detail": [{"msg": "bad"}]})

        with make_client(h) as c, pytest.raises(InvalidInput):
            c.tasks.steps(10)


class TestWait:
    def test_multi_poll_terminal(
        self, make_client: Callable[..., XAgentClient]
    ) -> None:
        calls = {"n": 0}

        def h(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            status = "running" if calls["n"] < 3 else "completed"
            output = None if status == "running" else "done"
            ca = None if status == "running" else "2026-05-10T03:00:05Z"
            return httpx.Response(
                200,
                json={
                    "task_id": 10,
                    "agent_id": 7,
                    "status": status,
                    "input": "hi",
                    "output": output,
                    "error": None,
                    "created_at": "2026-05-10T03:00:00Z",
                    "completed_at": ca,
                },
            )

        with make_client(h) as c:
            info = c.tasks.wait(10, timeout=2.0, poll_interval=0.01)

        assert info.status is TaskStatus.COMPLETED
        assert calls["n"] == 3

    def test_immediate_terminal(self, make_client: Callable[..., XAgentClient]) -> None:
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "task_id": 10,
                    "agent_id": 7,
                    "status": "completed",
                    "input": "hi",
                    "output": "done",
                    "error": None,
                    "created_at": "2026-05-10T03:00:00Z",
                    "completed_at": "2026-05-10T03:00:00Z",
                },
            )

        # poll_interval intentionally huge: should never sleep.
        with make_client(h) as c:
            info = c.tasks.wait(10, timeout=2.0, poll_interval=10.0)

        assert info.status is TaskStatus.COMPLETED

    def test_timeout(self, make_client: Callable[..., XAgentClient]) -> None:
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "task_id": 10,
                    "agent_id": 7,
                    "status": "running",
                    "input": "hi",
                    "output": None,
                    "error": None,
                    "created_at": "2026-05-10T03:00:00Z",
                    "completed_at": None,
                },
            )

        with make_client(h) as c, pytest.raises(TaskTimeout) as excinfo:
            c.tasks.wait(10, timeout=0.1, poll_interval=0.02)
        # last observed status is folded into the message for debug.
        assert "running" in excinfo.value.message

    def test_paused_keeps_polling(
        self, make_client: Callable[..., XAgentClient]
    ) -> None:
        # PAUSED is NOT terminal (mirrors backend `v1/tasks.py:170`);
        # wait() should poll until the deadline elapses.
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "task_id": 10,
                    "agent_id": 7,
                    "status": "paused",
                    "input": "hi",
                    "output": None,
                    "error": None,
                    "created_at": "2026-05-10T03:00:00Z",
                    "completed_at": None,
                },
            )

        with make_client(h) as c, pytest.raises(TaskTimeout) as excinfo:
            c.tasks.wait(10, timeout=0.1, poll_interval=0.02)
        assert "paused" in excinfo.value.message

    def test_propagates_404(self, make_client: Callable[..., XAgentClient]) -> None:
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                json={"error": {"code": "task_not_found", "message": "gone"}},
            )

        with make_client(h) as c, pytest.raises(TaskNotFound):
            c.tasks.wait(999, timeout=1.0, poll_interval=0.05)

    def test_wall_clock_cap(self, make_client: Callable[..., XAgentClient]) -> None:
        # poll_interval >> remaining timeout. wait() must cap sleep so the
        # wall-clock does not overshoot. Bug version would take ~5s; we
        # assert <0.5s (~5x the configured timeout, well below the 5s
        # bug baseline; safe on slow CI runners).
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "task_id": 10,
                    "agent_id": 7,
                    "status": "running",
                    "input": "hi",
                    "output": None,
                    "error": None,
                    "created_at": "2026-05-10T03:00:00Z",
                    "completed_at": None,
                },
            )

        with make_client(h) as c:
            t0 = time.monotonic()
            with pytest.raises(TaskTimeout):
                c.tasks.wait(10, timeout=0.1, poll_interval=5.0)
            elapsed = time.monotonic() - t0

        assert elapsed < 0.5

    def test_negative_timeout_rejected(
        self, make_client: Callable[..., XAgentClient]
    ) -> None:
        # The handler is never invoked; validation rejects before any HTTP.
        with (
            make_client(lambda req: httpx.Response(200)) as c,  # noqa: ARG005
            pytest.raises(ValueError, match="timeout"),
        ):
            c.tasks.wait(10, timeout=-1.0, poll_interval=0.05)

    def test_negative_poll_interval_rejected(
        self, make_client: Callable[..., XAgentClient]
    ) -> None:
        with (
            make_client(lambda req: httpx.Response(200)) as c,  # noqa: ARG005
            pytest.raises(ValueError, match="poll_interval"),
        ):
            c.tasks.wait(10, timeout=1.0, poll_interval=-0.05)


class TestRun:
    def test_full_flow(self, make_client: Callable[..., XAgentClient]) -> None:
        state = {"polls": 0}

        def h(req: httpx.Request) -> httpx.Response:
            method, path = req.method, req.url.path
            if method == "POST" and path == "/v1/chat/tasks":
                return httpx.Response(
                    202,
                    json={
                        "task_id": 42,
                        "agent_id": 7,
                        "status": "pending",
                        "created_at": "2026-05-10T03:00:00Z",
                    },
                )
            if method == "GET" and path == "/v1/chat/tasks/42":
                state["polls"] += 1
                status = "running" if state["polls"] < 2 else "completed"
                output = None if status == "running" else "final"
                ca = None if status == "running" else "2026-05-10T03:00:01Z"
                return httpx.Response(
                    200,
                    json={
                        "task_id": 42,
                        "agent_id": 7,
                        "status": status,
                        "input": "hi",
                        "output": output,
                        "error": None,
                        "created_at": "2026-05-10T03:00:00Z",
                        "completed_at": ca,
                    },
                )
            if method == "GET" and path == "/v1/chat/tasks/42/steps":
                return httpx.Response(
                    200,
                    json={
                        "task_id": 42,
                        "agent_id": 7,
                        "steps": [
                            {
                                "id": "message:1",
                                "type": "message",
                                "status": "completed",
                                "started_at": "2026-05-10T03:00:01Z",
                                "completed_at": "2026-05-10T03:00:01Z",
                                "data": {
                                    "role": "assistant",
                                    "content": "final",
                                },
                            }
                        ],
                    },
                )
            return httpx.Response(404)

        with make_client(h) as c:
            result = c.tasks.run(
                agent_id=7,
                message="hi",
                timeout=2.0,
                poll_interval=0.01,
            )

        assert isinstance(result, RunResult)
        assert result.output == "final"
        assert result.status is TaskStatus.COMPLETED
        assert len(result.steps) == 1
        assert result.steps[0].type is StepType.MESSAGE

    def test_timeout_propagates(self, make_client: Callable[..., XAgentClient]) -> None:
        def h(req: httpx.Request) -> httpx.Response:
            method, path = req.method, req.url.path
            if method == "POST" and path == "/v1/chat/tasks":
                return httpx.Response(
                    202,
                    json={
                        "task_id": 42,
                        "agent_id": 7,
                        "status": "pending",
                        "created_at": "2026-05-10T03:00:00Z",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "task_id": 42,
                    "agent_id": 7,
                    "status": "running",
                    "input": "hi",
                    "output": None,
                    "error": None,
                    "created_at": "2026-05-10T03:00:00Z",
                    "completed_at": None,
                },
            )

        with make_client(h) as c, pytest.raises(TaskTimeout):
            c.tasks.run(
                agent_id=7,
                message="hi",
                timeout=0.1,
                poll_interval=0.02,
            )

    def test_shared_deadline(self, make_client: Callable[..., XAgentClient]) -> None:
        # Inject latency into create() so that the time it consumes is
        # observable. Old behavior passed the full timeout to wait(),
        # producing total elapsed of (create_delay + timeout). New
        # behavior subtracts create's elapsed, so total is bounded by
        # timeout itself.
        create_delay = 0.1

        def h(req: httpx.Request) -> httpx.Response:
            method, path = req.method, req.url.path
            if method == "POST" and path == "/v1/chat/tasks":
                time.sleep(create_delay)
                return httpx.Response(
                    202,
                    json={
                        "task_id": 42,
                        "agent_id": 7,
                        "status": "pending",
                        "created_at": "2026-05-10T03:00:00Z",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "task_id": 42,
                    "agent_id": 7,
                    "status": "running",
                    "input": "hi",
                    "output": None,
                    "error": None,
                    "created_at": "2026-05-10T03:00:00Z",
                    "completed_at": None,
                },
            )

        timeout = 0.2
        with make_client(h) as c:
            t0 = time.monotonic()
            with pytest.raises(TaskTimeout):
                c.tasks.run(
                    agent_id=7,
                    message="hi",
                    timeout=timeout,
                    poll_interval=0.02,
                )
            elapsed = time.monotonic() - t0

        # Old (broken) behavior: elapsed ≈ create_delay + timeout ≈ 0.3s
        # New (correct) behavior: elapsed ≈ timeout ≈ 0.2s
        # Assert < timeout + create_delay/2 so we strictly distinguish.
        assert elapsed < timeout + create_delay / 2
