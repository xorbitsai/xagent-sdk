"""Mechanical pin for the SDK public surface.

If a future change silently widens (or narrows) ``xagent_sdk.__all__``
the test below fails and the diff has to either justify the surface
change or roll it back. The ``XAgentClient`` / ``MeResponse`` checks
guard against a re-export accidentally re-introducing names that the
package does not expose.
"""

import xagent_sdk

# The full public surface. Update this set deliberately when adding new
# exports; do not loosen the assertion to a subset check.
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
    # Runtime dataclasses
    "CreateTaskResult",
    "AppendResult",
    "TaskInfo",
    "PendingInteraction",
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
    "InteractionResponseRequired",
    "NoPendingInteraction",
    "InteractionNotResumable",
    "TemporarilyUnavailable",
    "RateLimited",
    "InternalError",
    "InvalidInput",
    "TemplateNotFound",
    "MalformedResponse",
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


def test_xagent_client_name_not_exposed() -> None:
    assert not hasattr(xagent_sdk, "XAgentClient"), (
        "the runtime client is named AgentClient; XAgentClient must not "
        "resolve via the public package"
    )
    assert "XAgentClient" not in xagent_sdk.__all__


def test_meresponse_name_not_exposed() -> None:
    assert not hasattr(xagent_sdk, "MeResponse"), (
        "the /v1/me payload is UserPrincipal; MeResponse must not "
        "resolve via the public package"
    )
    assert "MeResponse" not in xagent_sdk.__all__


def test_version_matches_pyproject() -> None:
    # The version string the SDK announces (also in the User-Agent
    # header) must match the packaged release.
    assert xagent_sdk.__version__ == "0.3.1"
