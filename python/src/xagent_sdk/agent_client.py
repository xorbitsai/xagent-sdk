import httpx

from xagent_sdk._base import _BaseClient
from xagent_sdk.tasks import TasksAPI


class AgentClient(_BaseClient):
    """Synchronous runtime client for the xAgent v1 chat-task endpoints.

    Authenticates with an **agent runtime key** (``xag_<prefix>_<secret>``)
    issued by ``UserClient.agents.create()`` /
    ``UserClient.agents.rotate_key()``. Each key is bound 1:1 to an agent
    and only authorizes the ``/v1/chat/tasks/*`` surface; management
    endpoints (``/v1/me``, ``/v1/templates*``, ``/v1/agents*``) require
    a personal key handled by ``UserClient`` instead.

    Constructor argument resolution order for ``api_key`` and ``base_url``:
      1. Explicit keyword argument
      2. Environment variable (``XAGENT_API_KEY`` / ``XAGENT_BASE_URL``)
      3. (v0.3.0+) Hardcoded production default URL -- not yet baked in
         while the xAgent team finalizes the prod endpoint.

    Missing values at construction time raise ``ValueError`` instead of
    deferring failure to the first request.

    The client owns one ``httpx.Client`` (connection pool) for its
    lifetime. Use it as a context manager or call ``close()`` explicitly
    to release the pool.

    Thread-safe: a single ``AgentClient`` can be shared across threads.
    Not fork-safe: close and recreate the client after ``os.fork()`` to
    avoid socket state corruption (a standard caveat for any HTTP client
    with a persistent connection pool).

    The ``transport`` parameter accepts any ``httpx.BaseTransport``,
    letting advanced users plug in custom retry, proxy, or TLS
    configuration; ``httpx.MockTransport`` also works for tests.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        timeout: float = 30.0,
        max_connections: int = 10,
        user_agent: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_connections=max_connections,
            user_agent=user_agent,
            transport=transport,
        )
        self.tasks = TasksAPI(self)
