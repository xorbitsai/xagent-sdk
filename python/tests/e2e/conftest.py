"""Fixtures for end-to-end tests against a real xAgent backend.

Two pairs of fixtures cover the two-client surface:

- ``user_client`` / ``patient_user_client`` -- ``UserClient``
  authenticated with ``XAGENT_PERSONAL_KEY``. Use for management
  endpoints (``/v1/me``, ``/v1/templates*``, ``/v1/agents*``).

- ``agent_client`` / ``patient_agent_client`` -- ``AgentClient``
  authenticated with ``XAGENT_API_KEY`` (an agent runtime key). Use
  for ``/v1/chat/tasks/*`` calls. ``patient_*`` variants raise the
  per-request HTTP timeout from 30s to 60s so tests can observe slow
  POST behavior (e.g. the async-contract regression catcher).

Run with::

    XAGENT_BASE_URL=http://localhost:8000 \\
    XAGENT_PERSONAL_KEY=xag_personal_... \\
    XAGENT_API_KEY=xag_... \\
        uv run pytest -m e2e

Fixtures call ``pytest.skip(...)`` when their required env vars are
missing, so a developer who only has one of the keys can still
exercise the other half of the surface. CI does not run e2e tests.
"""

import os
from collections.abc import Iterator

import pytest

from xagent_sdk import AgentClient, UserClient
from xagent_sdk.cloud import Region, WorkspaceClient


def _cloud_base_url() -> str | None:
    """Resolve the cloud host for e2e: explicit ``XAGENT_BASE_URL`` wins,
    else the host for ``XAGENT_REGION`` (``au``/``sg``). Returns ``None``
    when neither is set -- there is no hosted default to guess, since the
    service is per-region.
    """
    explicit = os.environ.get("XAGENT_BASE_URL")
    if explicit:
        return explicit
    region = os.environ.get("XAGENT_REGION")
    if region:
        return Region(region.strip().lower()).base_url
    return None


def _need_workspace() -> tuple[str, str]:
    workspace_key = os.environ.get("XAGENT_WORKSPACE_KEY")
    base_url = _cloud_base_url()
    if not (workspace_key and base_url):
        pytest.skip(
            "e2e cloud surface requires XAGENT_WORKSPACE_KEY and "
            "XAGENT_BASE_URL or XAGENT_REGION"
        )
    return workspace_key, base_url


def _need_personal() -> tuple[str, str]:
    api_key = os.environ.get("XAGENT_PERSONAL_KEY")
    base_url = os.environ.get("XAGENT_BASE_URL")
    if not (api_key and base_url):
        pytest.skip(
            "e2e management surface requires XAGENT_PERSONAL_KEY + XAGENT_BASE_URL"
        )
    return api_key, base_url


def _need_runtime() -> tuple[str, str]:
    api_key = os.environ.get("XAGENT_API_KEY")
    base_url = os.environ.get("XAGENT_BASE_URL")
    if not (api_key and base_url):
        pytest.skip("e2e runtime surface requires XAGENT_API_KEY + XAGENT_BASE_URL")
    return api_key, base_url


@pytest.fixture
def user_client() -> Iterator[UserClient]:
    """UserClient with the SDK's 30s per-request HTTP timeout."""
    personal_key, base_url = _need_personal()
    with UserClient(personal_key=personal_key, base_url=base_url) as c:
        yield c


@pytest.fixture
def patient_user_client() -> Iterator[UserClient]:
    """UserClient with a 60s per-request HTTP timeout for slow probes."""
    personal_key, base_url = _need_personal()
    with UserClient(personal_key=personal_key, base_url=base_url, timeout=60.0) as c:
        yield c


@pytest.fixture
def agent_client() -> Iterator[AgentClient]:
    """AgentClient with the SDK's 30s per-request HTTP timeout."""
    api_key, base_url = _need_runtime()
    with AgentClient(api_key=api_key, base_url=base_url) as c:
        yield c


@pytest.fixture
def patient_agent_client() -> Iterator[AgentClient]:
    """AgentClient with a 60s per-request HTTP timeout.

    Used by tests that want to *observe* an operation whose latency may
    exceed the SDK default (e.g. measuring whether POST is actually
    async). With the default 30s, a synchronous backend implementation
    would surface as a generic ``XAgentTransportError: timed out``;
    this fixture lets the call complete so the test can assert on the
    measured latency itself, giving a clearer regression signal.
    """
    api_key, base_url = _need_runtime()
    with AgentClient(api_key=api_key, base_url=base_url, timeout=60.0) as c:
        yield c


@pytest.fixture
def cloud_base_url() -> str:
    """Resolved cloud host for tests that build their own client (e.g. a
    bad-key probe). Skips when no ``XAGENT_BASE_URL`` / ``XAGENT_REGION``
    is set, since the service is per-region and has no default host.
    """
    base_url = _cloud_base_url()
    if base_url is None:
        pytest.skip("e2e cloud surface requires XAGENT_BASE_URL or XAGENT_REGION")
    return base_url


@pytest.fixture
def workspace_client() -> Iterator[WorkspaceClient]:
    """WorkspaceClient authenticated with a workspace key.

    Requires ``XAGENT_WORKSPACE_KEY`` plus an explicit host -- either
    ``XAGENT_BASE_URL`` (staging / self-host) or ``XAGENT_REGION``
    (``au``/``sg``). The service is per-region, so a key only authenticates
    against the region that minted it; there is no default host to fall
    back to. 60s per-request timeout so agent runs created here have room
    to complete.
    """
    workspace_key, base_url = _need_workspace()
    with WorkspaceClient(
        workspace_key=workspace_key, base_url=base_url, timeout=60.0
    ) as c:
        yield c
