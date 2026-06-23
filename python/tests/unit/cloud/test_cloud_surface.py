"""Pin for the cloud submodule public surface.

The cloud submodule exposes ``WorkspaceClient`` and ``Region`` and is not
re-exported from the top-level package: importing ``xagent_sdk`` must not
pull in cloud or surface these at the top level.
"""

import xagent_sdk
import xagent_sdk.cloud


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
