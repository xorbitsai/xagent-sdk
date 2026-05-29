import httpx

from xagent_sdk._agents import AgentsAPI
from xagent_sdk._base import _BaseClient
from xagent_sdk._templates import TemplatesAPI
from xagent_sdk.types import UserPrincipal, _parse_user_principal


class UserClient(_BaseClient):
    """Synchronous management client for the xAgent v1 user-facing surface.

    Authenticates with a **personal key** (``xag_personal_<prefix>_<secret>``
    issued by the xAgent web UI to an individual user). Personal keys are
    workspace-wide for the user and authorize the management surface
    ``/v1/me`` + ``/v1/templates*`` + ``/v1/agents*``; they cannot drive
    chat tasks. Use ``AgentClient`` -- with an **agent runtime key**
    minted by ``UserClient.agents.create()`` /
    ``UserClient.agents.rotate_key()`` -- to invoke
    ``/v1/chat/tasks/*``.

    Constructor argument resolution order for ``personal_key`` and
    ``base_url``:

    1. Explicit keyword argument
    2. Environment variable
       (``XAGENT_PERSONAL_KEY`` / ``XAGENT_BASE_URL``)

    Missing values at construction time raise ``ValueError`` instead of
    deferring failure to the first request. ``XAGENT_PERSONAL_KEY`` is a
    separate env var from ``XAGENT_API_KEY`` (which feeds ``AgentClient``)
    so the two clients can coexist in the same process without sharing
    state.

    The ``transport`` parameter accepts any ``httpx.BaseTransport`` for
    proxy / TLS / test injection -- ``httpx.MockTransport`` is the
    canonical unit-test path.
    """

    _ENV_API_KEY = "XAGENT_PERSONAL_KEY"
    _API_KEY_FIELD = "personal_key"

    def __init__(
        self,
        personal_key: str | None = None,
        base_url: str | None = None,
        *,
        timeout: float = 30.0,
        max_connections: int = 10,
        user_agent: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(
            api_key=personal_key,
            base_url=base_url,
            timeout=timeout,
            max_connections=max_connections,
            user_agent=user_agent,
            transport=transport,
        )
        self.templates = TemplatesAPI(self)
        self.agents = AgentsAPI(self)

    def me(self) -> UserPrincipal:
        """``GET /v1/me`` -- identity probe for the personal key.

        Zero side-effect. Returns the user principal the personal key
        belongs to (``principal_type`` / ``user_id`` / ``email`` /
        ``name`` / ``key_prefix``). Use once at startup to log which
        user is connected; cache the result locally if you only need it
        once -- the SDK does not cache so a revoked key surfaces as
        ``InvalidAPIKey`` immediately rather than silently using a
        stale principal.
        """
        resp = self._request("GET", "/v1/me")
        return _parse_user_principal(resp.json())
