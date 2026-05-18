"""End-to-end smoke test against a real xAgent backend.

Marked ``@pytest.mark.e2e`` so the default ``pytest`` invocation skips
it. Run explicitly with::

    XAGENT_BASE_URL=... XAGENT_API_KEY=... uv run pytest -m e2e

Set ``E2E_AGENT_ID`` to override the default agent the smoke test
points at (otherwise the test calls ``me()`` to discover the agent
bound to the presented key).
"""

import os

import pytest

from xagent_sdk import RunResult, TaskStatus, XAgentClient

pytestmark = pytest.mark.e2e


def test_me(client: XAgentClient) -> None:
    me = client.me()
    assert me.agent_id > 0
    assert me.agent_name
    assert me.key_prefix


def test_run_single_turn(client: XAgentClient) -> None:
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
