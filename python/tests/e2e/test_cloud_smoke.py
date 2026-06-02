"""End-to-end smoke tests for the cloud / workspace surface.

Marked ``@pytest.mark.e2e`` so the default ``pytest`` run skips them.
Run against a SaaS deploy with a workspace key::

    XAGENT_WORKSPACE_KEY=xag_workspace_... uv run pytest -m e2e
    # XAGENT_BASE_URL optional; defaults to the hosted endpoint

``E2E_TEMPLATE_ID`` picks the template to instantiate (defaults to the
first listed). The created agent is not deleted -- run on a scratch
workspace.
"""

import os

import pytest

from xagent_sdk import (
    AgentClient,
    InvalidAPIKey,
    RunResult,
    TaskStatus,
    TemplateNotFound,
)
from xagent_sdk.cloud import WorkspaceClient

pytestmark = pytest.mark.e2e


def _runtime_base_url() -> str:
    # The minted runtime key drives the existing /v1/chat/tasks* surface
    # on the same host the workspace client targets.
    return os.environ.get("XAGENT_BASE_URL") or "https://cloud.xagent.run"


def test_workspace_full_flow(workspace_client: WorkspaceClient) -> None:
    templates = workspace_client.templates.list()
    if not templates:
        pytest.skip("workspace exposes no templates; nothing to instantiate")
    template_id = os.environ.get("E2E_TEMPLATE_ID", templates[0].template_id)

    created = workspace_client.agents.create_from_template(
        template_id, name=f"cloud_smoke_{os.getpid()}"
    )
    assert created.agent_id > 0
    assert created.runtime_full_key is not None
    assert created.runtime_full_key.startswith("xag_")

    with AgentClient(
        api_key=created.runtime_full_key, base_url=_runtime_base_url(), timeout=120.0
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


def test_unknown_template_raises(workspace_client: WorkspaceClient) -> None:
    with pytest.raises(TemplateNotFound):
        workspace_client.agents.create_from_template("does-not-exist-12345")


def test_bad_workspace_key_unauthorized() -> None:
    base_url = os.environ.get("XAGENT_BASE_URL")
    if not os.environ.get("XAGENT_WORKSPACE_KEY"):
        pytest.skip("e2e workspace surface requires XAGENT_WORKSPACE_KEY")
    with (
        WorkspaceClient(workspace_key="xag_workspace_bad_key", base_url=base_url) as c,
        pytest.raises(InvalidAPIKey),
    ):
        c.agents.list()


def test_create_without_runtime_key(workspace_client: WorkspaceClient) -> None:
    created = workspace_client.agents.create(
        name=f"cloud_smoke_nokey_{os.getpid()}",
        instructions="Echo one line.",
        generate_runtime_key=False,
    )
    assert created.agent_id > 0
    assert created.runtime_full_key is None
