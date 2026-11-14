"""End-to-end check of ``UserClient.agents.get_connectors()`` and
``replace_connectors()`` against a real backend.

Besides ``XAGENT_BASE_URL`` and ``XAGENT_PERSONAL_KEY`` the test needs
``XAGENT_E2E_MCP_URL``: the URL of a streamable_http MCP server the backend
may reach. Without the variable it is skipped; with it, a failure to set the
connector up fails the test instead of skipping it.

The test creates an agent and a connector with unique names through the
backend. The connector is deleted by the test. The ``/v1`` API has no way to
delete an agent, so the agent stays behind (the same as in
``test_cloud_smoke.py``). What stays is a personal agent without a runtime
key, whose connector selection has been cleared.
"""

import contextlib
import os
import uuid

import httpx
import pytest

from xagent_sdk import UserClient

pytestmark = pytest.mark.e2e


def test_read_and_replace_an_agents_connectors(user_client: UserClient) -> None:
    mcp_url = os.environ.get("XAGENT_E2E_MCP_URL")
    base_url = os.environ.get("XAGENT_BASE_URL")
    key = os.environ.get("XAGENT_PERSONAL_KEY")
    if not (mcp_url and base_url and key):
        pytest.skip(
            "agent connectors e2e requires XAGENT_E2E_MCP_URL, XAGENT_BASE_URL "
            "and XAGENT_PERSONAL_KEY"
        )
    suffix = uuid.uuid4().hex[:8]
    connector_name = f"sdk-e2e-connector-{suffix}"
    api = user_client.agents
    agent_id: int | None = None
    server_id: int | None = None
    with httpx.Client(
        base_url=base_url, headers={"Authorization": f"Bearer {key}"}, timeout=30.0
    ) as http:
        try:
            created = api.create(
                name=f"sdk-e2e-connectors-{suffix}",
                instructions="Created by the SDK end-to-end tests.",
                generate_runtime_key=False,
            )
            agent_id = created.agent_id
            if created.runtime_full_key is not None:
                pytest.fail("agent was created with a runtime key")

            made = http.post(
                "/v1/mcp-servers",
                json={
                    "name": connector_name,
                    "transport": "streamable_http",
                    "url": mcp_url,
                },
            )
            assert made.status_code == 200, made.text
            server_id = made.json()["id"]

            # A new agent has chosen nothing.
            before = api.get_connectors(agent_id)
            assert before.agent_id == agent_id
            assert before.connectors == []
            assert before.all_connectors is False
            assert before.unresolved == []

            written = api.replace_connectors(
                agent_id, [connector_name], all_connectors=False
            )
            assert written.connectors == [connector_name]
            assert written.all_connectors is False
            assert written.unresolved == []

            after = api.get_connectors(agent_id)
            assert after == written

            # Sending the selection back unchanged is accepted and changes
            # nothing.
            again = api.replace_connectors(
                agent_id, after.connectors, all_connectors=after.all_connectors
            )
            assert again == written

            # Delete the connector: its name stays on the agent, and the
            # agent now reports it as one the key holder cannot see.
            deleted = http.delete(f"/v1/mcp-servers/{server_id}")
            assert deleted.status_code == 200, deleted.text
            server_id = None
            orphaned = api.get_connectors(agent_id)
            assert connector_name in orphaned.connectors
            assert connector_name in orphaned.unresolved

            # Removing a name that cannot be seen any more is not refused.
            cleared = api.replace_connectors(agent_id, [], all_connectors=False)
            assert cleared.connectors == []
            assert cleared.all_connectors is False
            assert cleared.unresolved == []
        finally:
            # Best effort, each step on its own so one failure does not skip
            # the other.
            if agent_id is not None:
                with contextlib.suppress(Exception):
                    api.replace_connectors(agent_id, [], all_connectors=False)
            if server_id is not None:
                with contextlib.suppress(Exception):
                    http.delete(f"/v1/mcp-servers/{server_id}")
