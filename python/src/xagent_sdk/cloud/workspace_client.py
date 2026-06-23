import httpx

from xagent_sdk._base import _BaseClient
from xagent_sdk.cloud._agents import WorkspaceAgentsAPI
from xagent_sdk.cloud._templates import WorkspaceTemplatesAPI
from xagent_sdk.cloud.region import _REGION_BASE_URL, Region


class WorkspaceClient(_BaseClient):
    """Synchronous client for the hosted xAgent workspace surface.

    Authenticates with a **workspace key** (``xag_workspace_<prefix>_<secret>``
    issued by the hosted app to a workspace/team). A workspace key
    authorizes managing agents and reading templates for that workspace:
    ``/v1/workspace/templates*`` and ``/v1/workspace/agents*``. It does
    not run agents -- create or mint a runtime key here, then drive
    ``/v1/chat/tasks*`` with ``AgentClient`` and that runtime key.

    Constructor argument resolution:

    - ``workspace_key``: explicit keyword, else ``XAGENT_WORKSPACE_KEY``.
      A separate variable from ``XAGENT_API_KEY`` / ``XAGENT_PERSONAL_KEY``
      so the clients can coexist in one process. An explicitly empty key
      raises rather than falling back to the environment.
    - Target host: pass **either** ``region`` (the ``Region`` shown in the
      deploy snippet -- the hosted service is per-region and a workspace
      key only works against the region that issued it) **or** an explicit
      ``base_url`` (for a self-hosted or not-yet-listed region), but not
      both. With neither, ``XAGENT_BASE_URL`` is used; if that is unset the
      constructor raises rather than guessing a host.

    Missing values at construction raise ``ValueError`` instead of
    deferring failure to the first request. ``transport`` accepts any
    ``httpx.BaseTransport`` for proxy / TLS / test injection.
    """

    _ENV_API_KEY = "XAGENT_WORKSPACE_KEY"
    _API_KEY_FIELD = "workspace_key"
    _BASE_URL_HINT = "pass region=... (or base_url=...) or set XAGENT_BASE_URL"

    def __init__(
        self,
        workspace_key: str | None = None,
        *,
        region: Region | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_connections: int = 10,
        user_agent: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if region is not None:
            if base_url is not None:
                raise ValueError("pass region or base_url, not both")
            base_url = _REGION_BASE_URL[region]
        super().__init__(
            api_key=workspace_key,
            base_url=base_url,
            timeout=timeout,
            max_connections=max_connections,
            user_agent=user_agent,
            transport=transport,
        )
        self.agents = WorkspaceAgentsAPI(self)
        self.templates = WorkspaceTemplatesAPI(self)
