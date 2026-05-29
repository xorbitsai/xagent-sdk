"""End-to-end smoke tests against a real xAgent backend (0.2.0).

Marked ``@pytest.mark.e2e`` so the default ``pytest`` invocation skips
the whole file. Run explicitly with::

    XAGENT_BASE_URL=... XAGENT_PERSONAL_KEY=... XAGENT_API_KEY=... \\
        uv run pytest -m e2e

Set ``E2E_AGENT_ID`` to override the agent id used by runtime-only
tests; otherwise the runtime tests will skip when the variable is
unset, since 0.2.0 ``AgentClient`` no longer exposes a self-identity
probe (use the personal-key path / ``UserClient.agents.list()`` to
discover agent ids).

Set ``E2E_TEMPLATE_ID`` (default ``q_and_a``) to pick which template
the full-flow test instantiates an agent from. ``E2E_AGENT_NAME``
(default ``e2e_smoke_<pid>``) controls the new agent's display name.
"""

import os
import time

import pytest

from xagent_sdk import (
    AgentClient,
    RunResult,
    TaskStatus,
    UserClient,
    UserPrincipal,
)

pytestmark = pytest.mark.e2e


def test_user_me(user_client: UserClient) -> None:
    me = user_client.me()
    assert isinstance(me, UserPrincipal)
    assert me.principal_type == "user"
    assert me.user_id > 0
    assert me.email
    assert me.name
    assert me.key_prefix


def test_create_is_async(patient_agent_client: AgentClient) -> None:
    """POST /v1/chat/tasks must return asynchronously per v1 contract.

    Contract is "create the task, return 202 immediately, run LLM in
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
    agent_id_env = os.environ.get("E2E_AGENT_ID")
    if not agent_id_env:
        pytest.skip("E2E_AGENT_ID not set; cannot exercise runtime path")
    agent_id = int(agent_id_env)

    t0 = time.monotonic()
    created = patient_agent_client.tasks.create(agent_id=agent_id, message="Say hi")
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


def test_run_single_turn(agent_client: AgentClient) -> None:
    """Single-turn runtime probe with an existing agent.

    Requires ``E2E_AGENT_ID`` because 0.2.0 ``AgentClient`` no longer
    has an identity probe; the test would have nothing to point at
    without a caller-supplied id.
    """
    agent_id_env = os.environ.get("E2E_AGENT_ID")
    if not agent_id_env:
        pytest.skip("E2E_AGENT_ID not set; cannot exercise runtime path")
    agent_id = int(agent_id_env)
    result = agent_client.tasks.run(
        agent_id=agent_id,
        message="Say hi in one word",
        timeout=60.0,
        poll_interval=2.0,
    )
    assert isinstance(result, RunResult)
    assert result.status is TaskStatus.COMPLETED
    assert result.output is not None


def test_e2e_full_flow_phase2(user_client: UserClient) -> None:
    """End-to-end Phase 2 flow: pick a template, create an agent, run it.

    1. List templates and confirm at least one entry exists.
    2. Pick ``E2E_TEMPLATE_ID`` (default ``q_and_a``) and call
       ``agents.create_from_template`` to mint a fresh agent + runtime
       key.
    3. Build an ``AgentClient`` with the freshly minted runtime key.
    4. Drive a single-turn ``tasks.run()`` against the new agent and
       assert it completes.

    The test deliberately does **not** clean up the created agent --
    Phase 2 SDK has no delete method, and the backend's own
    housekeeping owns the lifecycle. Run on a scratch backend instance.
    """
    template_id = os.environ.get("E2E_TEMPLATE_ID", "q_and_a")
    agent_name = os.environ.get("E2E_AGENT_NAME", f"e2e_smoke_{os.getpid()}")
    base_url = os.environ["XAGENT_BASE_URL"]

    templates = user_client.templates.list()
    assert templates, "backend returned an empty template list"

    created = user_client.agents.create_from_template(
        template_id, overrides={"name": agent_name}
    )
    assert created.agent_id > 0
    # generate_runtime_key defaults to True; one-time secret must be
    # present so the next step has something to authenticate with.
    assert created.runtime_full_key is not None
    assert created.runtime_full_key.startswith("xag_")

    with AgentClient(
        api_key=created.runtime_full_key, base_url=base_url, timeout=60.0
    ) as runtime:
        result = runtime.tasks.run(
            agent_id=created.agent_id,
            message="Say hi in one word",
            timeout=120.0,
            poll_interval=2.0,
        )
    assert isinstance(result, RunResult)
    assert result.status is TaskStatus.COMPLETED
    assert result.output is not None
