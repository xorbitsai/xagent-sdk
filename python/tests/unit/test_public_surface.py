"""Mechanical pin for the SDK 0.2.0 public surface.

If a future PR silently widens (or narrows) ``xagent_sdk.__all__`` the
test below fails and the diff has to either justify the surface change
or roll it back. The legacy-name checks (``XAgentClient`` /
``MeResponse``) verify the 0.1.0 -> 0.2.0 breaking rename so a
re-export sneaking back in trips CI.
"""

import xagent_sdk

# The full public 0.2.0 surface. Update this set deliberately when
# adding new exports; do not loosen the assertion to a subset check.
EXPECTED_SURFACE: set[str] = {
    # Clients
    "AgentClient",
    "UserClient",
    # User principal + management dataclasses
    "UserPrincipal",
    "Template",
    "TemplateDetail",
    "AgentSummary",
    "AgentCreateResult",
    "RotateKeyResult",
    # Runtime dataclasses (unchanged from 0.1.0)
    "CreateTaskResult",
    "AppendResult",
    "TaskInfo",
    "Step",
    "RunResult",
    # Enums
    "TaskStatus",
    "StepType",
    # Exception hierarchy
    "XAgentError",
    "InvalidAPIKey",
    "AgentNotFound",
    "TaskNotFound",
    "TaskBusy",
    "RateLimited",
    "InternalError",
    "InvalidInput",
    "TemplateNotFound",
    "XAgentTransportError",
    "TaskTimeout",
    # Version
    "__version__",
}


def test_all_matches_expected() -> None:
    actual = set(xagent_sdk.__all__)
    extra = actual - EXPECTED_SURFACE
    missing = EXPECTED_SURFACE - actual
    assert not extra, f"extra symbols in __all__: {sorted(extra)}"
    assert not missing, f"missing symbols from __all__: {sorted(missing)}"


def test_every_exported_name_resolves() -> None:
    # __all__ is just a tuple of strings; ensure each name is actually
    # importable from the package (catches typos / forgotten imports).
    for name in xagent_sdk.__all__:
        assert hasattr(xagent_sdk, name), (
            f"{name!r} listed in __all__ but not present on the package"
        )


def test_xagent_client_legacy_name_removed() -> None:
    assert not hasattr(xagent_sdk, "XAgentClient"), (
        "0.2.0 renamed XAgentClient -> AgentClient; the legacy name must "
        "not resolve via the public package"
    )
    assert "XAgentClient" not in xagent_sdk.__all__


def test_meresponse_legacy_name_removed() -> None:
    assert not hasattr(xagent_sdk, "MeResponse"), (
        "0.2.0 replaced MeResponse with UserPrincipal; the legacy name "
        "must not resolve via the public package"
    )
    assert "MeResponse" not in xagent_sdk.__all__


def test_version_bumped_to_0_2_0() -> None:
    # The release-strategy pin: 0.2.0 is a breaking release and the
    # version string the SDK announces (also in the User-Agent header)
    # must reflect that.
    assert xagent_sdk.__version__ == "0.2.0"
