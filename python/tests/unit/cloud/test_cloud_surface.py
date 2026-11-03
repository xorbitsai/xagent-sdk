"""Pin for the cloud submodule public surface.

The cloud submodule exposes ``WorkspaceClient`` and ``Region`` and is not
re-exported from the top-level package: importing ``xagent_sdk`` must not
pull in cloud or surface these at the top level.
"""

import httpx
import pytest

import xagent_sdk
import xagent_sdk.cloud
from xagent_sdk.cloud import WorkspaceClient


def test_cloud_all_is_exact_set() -> None:
    assert set(xagent_sdk.cloud.__all__) == {"WorkspaceClient", "Region"}


def test_cloud_names_resolve() -> None:
    from xagent_sdk.cloud import Region, WorkspaceClient

    assert WorkspaceClient.__name__ == "WorkspaceClient"
    assert {r.value for r in Region} == {"au", "sg"}


def test_cloud_names_not_on_top_level() -> None:
    for name in ("WorkspaceClient", "Region"):
        assert not hasattr(xagent_sdk, name)
        assert name not in xagent_sdk.__all__


@pytest.mark.parametrize("name", ["tasks", "skills"])
def test_workspace_client_exposes_no_tasks_or_skills(name: str) -> None:
    ws = WorkspaceClient(
        workspace_key="xag_workspace_p_s",
        base_url="https://test.example",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})),
    )
    try:
        assert not hasattr(ws, name)
    finally:
        ws.close()
