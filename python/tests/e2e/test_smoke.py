"""End-to-end smoke test against a real xAgent backend.

Marked ``@pytest.mark.e2e`` so the default ``pytest`` invocation skips
it. Run explicitly with::

    XAGENT_BASE_URL=... XAGENT_API_KEY=... uv run pytest -m e2e

Set ``E2E_AGENT_ID`` to override the default agent the smoke test
points at (otherwise the test calls ``me()`` to discover the agent
bound to the presented key).
"""

import os
import time

import pytest

from xagent_sdk import AgentClient, RunResult, TaskStatus

pytestmark = pytest.mark.e2e


def test_me(client: AgentClient) -> None:
    me = client.me()
    assert me.agent_id > 0
    assert me.agent_name
    assert me.key_prefix


def test_create_is_async(patient_client: AgentClient) -> None:
    """POST /v1/chat/tasks must return asynchronously per v1 contract.

    The contract is "create the task, return 202 immediately, run LLM in
    the background, observe transitions via GET poll". A backend that
    blocks POST until the LLM call completes can still produce correct
    final output via ``run()``, but defeats the design of having async
    polling: the SDK's ``wait()`` helper has no work to do because the
    task is already terminal by the time POST returns.

    Regression catcher: an earlier backend orchestrator refactor
    silently made POST synchronous, and our other e2e tests passed
    only because the LLM happened to finish within the SDK's 30s
    per-request HTTP timeout. This test enforces the timing contract
    explicitly so the failure points at the contract violation rather
    than surfacing as a generic transport timeout.
    """
    agent_id = int(os.environ.get("E2E_AGENT_ID", str(patient_client.me().agent_id)))

    t0 = time.monotonic()
    created = patient_client.tasks.create(agent_id=agent_id, message="Say hi")
    post_elapsed = time.monotonic() - t0

    assert created.status is TaskStatus.PENDING, (
        f"POST returned status={created.status.value!r}, expected 'pending'"
    )
    assert post_elapsed < 5.0, (
        f"POST took {post_elapsed:.2f}s; v1 contract requires async "
        f"return (typically <=1s in practice). A backend that "
        f"synchronously executes the task during POST violates "
        f"async-polling semantics even when the response body reads "
        f"status='pending'."
    )


def test_run_single_turn(client: AgentClient) -> None:
    agent_id = int(os.environ.get("E2E_AGENT_ID", str(client.me().agent_id)))
    result = client.tasks.run(
        agent_id=agent_id,
        message="Say hi in one word",
        timeout=60.0,
        poll_interval=2.0,
    )
    assert isinstance(result, RunResult)
    assert result.status is TaskStatus.COMPLETED
    assert result.output is not None
