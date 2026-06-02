"""Pin for the cloud submodule public surface.

The cloud submodule exposes exactly ``WorkspaceClient`` and is not
re-exported from the top-level package: importing ``xagent_sdk`` must not
pull in cloud or surface ``WorkspaceClient`` at the top level.
"""

import xagent_sdk
import xagent_sdk.cloud


def test_cloud_all_is_exactly_workspace_client() -> None:
    assert set(xagent_sdk.cloud.__all__) == {"WorkspaceClient"}


def test_workspace_client_resolves_from_cloud() -> None:
    from xagent_sdk.cloud import WorkspaceClient

    assert WorkspaceClient.__name__ == "WorkspaceClient"


def test_workspace_client_not_on_top_level() -> None:
    assert not hasattr(xagent_sdk, "WorkspaceClient")
    assert "WorkspaceClient" not in xagent_sdk.__all__
