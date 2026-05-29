import httpx

from xagent_sdk._base import _BaseClient
from xagent_sdk.tasks import TasksAPI
from xagent_sdk.types import MeResponse, _parse_me


class XAgentClient(_BaseClient):
    """Synchronous Python client for the xAgent v1 HTTP API.

    Constructor argument resolution order for ``api_key`` and ``base_url``:
      1. Explicit keyword argument
      2. Environment variable (``XAGENT_API_KEY`` / ``XAGENT_BASE_URL``)
      3. (v0.2.0+) Hardcoded production default URL -- not yet baked in
         while the xAgent team finalizes the prod endpoint.

    Missing values at construction time raise ``ValueError`` instead of
    deferring failure to the first request.

    The client owns one ``httpx.Client`` (connection pool) for its
    lifetime. Use it as a context manager or call ``close()`` explicitly
    to release the pool.

    Thread-safe: a single ``XAgentClient`` can be shared across threads.
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

    def me(self) -> MeResponse:
        """Probe the agent identity bound to the API key.

        Zero side-effect. Typically called once at startup to confirm the
        key is valid and log which agent the client is talking to.

        Each call hits the backend; if you only need the identity once,
        store the result (``identity = client.me()``) rather than
        re-calling.
        """
        resp = self._request("GET", "/v1/me")
        return _parse_me(resp.json())
