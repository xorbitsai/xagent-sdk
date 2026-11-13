"""End-to-end checks for ``UserClient.mcp_servers`` against a real backend.

Besides ``XAGENT_BASE_URL`` and ``XAGENT_PERSONAL_KEY`` these tests need
``XAGENT_E2E_MCP_URL``: the URL of a streamable_http MCP server the
backend may reach. The tests create a connector pointing at it, with a
unique name, through the raw HTTP API and delete it afterwards. Without
the variable they are skipped; with it, a failure to set the connector up
fails the test instead of skipping it.
"""

import os
import uuid
from collections.abc import Iterator

import httpx
import pytest

from xagent_sdk import ConnectorNotFound, UserClient

pytestmark = pytest.mark.e2e


@pytest.fixture
def seeded_connector() -> Iterator[dict[str, object]]:
    mcp_url = os.environ.get("XAGENT_E2E_MCP_URL")
    base_url = os.environ.get("XAGENT_BASE_URL")
    key = os.environ.get("XAGENT_PERSONAL_KEY")
    if not (mcp_url and base_url and key):
        pytest.skip(
            "MCP connector e2e requires XAGENT_E2E_MCP_URL, XAGENT_BASE_URL "
            "and XAGENT_PERSONAL_KEY"
        )
    name = f"sdk-e2e-{uuid.uuid4().hex[:8]}"
    with httpx.Client(
        base_url=base_url, headers={"Authorization": f"Bearer {key}"}, timeout=30.0
    ) as http:
        created = http.post(
            "/v1/mcp-servers",
            json={"name": name, "transport": "streamable_http", "url": mcp_url},
        )
        assert created.status_code == 200, created.text
        body: dict[str, object] = created.json()
        try:
            yield body
        finally:
            http.delete(f"/v1/mcp-servers/{body['id']}")


def test_list_and_get_a_connector(
    user_client: UserClient, seeded_connector: dict[str, object]
) -> None:
    server_id = seeded_connector["id"]
    assert isinstance(server_id, int)

    listed = {s.id: s for s in user_client.mcp_servers.list()}
    assert server_id in listed
    assert listed[server_id].access == "own"

    server = user_client.mcp_servers.get(server_id)
    assert server.name == seeded_connector["name"]
    assert server.enabled is True


def test_list_the_tools_of_a_connector(
    user_client: UserClient, seeded_connector: dict[str, object]
) -> None:
    server_id = seeded_connector["id"]
    assert isinstance(server_id, int)

    result = user_client.mcp_servers.list_tools(server_id)
    assert result.id == server_id
    assert result.tools
    assert all(tool.mcp_name for tool in result.tools)


def test_get_a_connector_that_does_not_exist(user_client: UserClient) -> None:
    with pytest.raises(ConnectorNotFound) as exc:
        user_client.mcp_servers.get(2**31 - 1)
    assert exc.value.http_status == 404
