import httpx

from xagent_sdk._agents import AgentsAPI
from xagent_sdk._base import _BaseClient
from xagent_sdk._templates import TemplatesAPI
from xagent_sdk.tasks import TasksAPI
from xagent_sdk.types import UserPrincipal, _parse_user_principal


class UserClient(_BaseClient):
    """Synchronous client for everything a personal key can do.

    Authenticates with a **personal key** (``xag_personal_<prefix>_<secret>``)
    issued by the xAgent web UI to an individual user. A personal key
    authorizes ``/v1/me`` (``me()``), ``/v1/templates*`` (``templates``),
    ``/v1/agents*`` (``agents``) and the ``/v1/chat/tasks*`` task endpoints
    (``tasks``).

    ``tasks`` starts tasks on any agent the key holder can see: the agents
    ``agents.list()`` returns. Whether the agent is published does not
    matter. A task created with a personal key runs as the key holder, not
    as the agent's owner: skill names on the agent are looked up among the
    skills the holder can see. The connectors the task can call are chosen,
    by the agent's tool settings, from the connectors the holder can use,
    which include team connectors where the deployment shares them; a
    connector only the agent's owner can use is not available, and its
    absence is not an error. So the same agent can behave differently under
    a personal key than under its agent key, and a connector error such as
    ``missing_runtime_context`` surfaces as ``InternalError`` carrying the
    server's code.

    Task visibility is per user, not per key: every personal key of a user
    can read every task that any of that user's personal keys created, and
    revoking one key does not hide those tasks from the user's other keys.
    This holds only while the task's agent exists: deleting the agent hides
    its tasks from every personal key. ``tasks.append()`` and
    ``tasks.reply()`` also require that the holder can still see the task's
    agent; ``tasks.get()``, ``tasks.steps()``, ``tasks.events()`` and
    ``tasks.wait()`` do not. Tasks created with an agent key are never
    visible to a personal key, and tasks created with a personal key are
    never visible to an agent key.

    A server that predates personal-key task support rejects a personal key
    on the task endpoints with ``InvalidAPIKey`` (HTTP 401), even though the
    same key works for ``me()``, ``templates`` and ``agents``.

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
        self.tasks = TasksAPI(self)

    def me(self) -> UserPrincipal:
        """``GET /v1/me`` -- identity probe for the personal key.

        Zero side-effect. Returns the user principal the personal key
        belongs to (``principal_type`` / ``user_id`` / ``username`` /
        ``email`` (nullable) / ``key_prefix``). Use once at startup to log
        which user is connected; cache the result locally if you only need
        it once -- the SDK does not cache so a revoked key surfaces as
        ``InvalidAPIKey`` immediately rather than silently using a
        stale principal.
        """
        resp = self._request("GET", "/v1/me")
        return _parse_user_principal(resp.json())
