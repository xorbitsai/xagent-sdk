import os
from types import TracebackType
from typing import Self

import httpx

from xagent_sdk._http import HTTPClient
from xagent_sdk.errors import from_response
from xagent_sdk.tasks import TasksAPI
from xagent_sdk.types import MeResponse, _parse_me


class XAgentClient:
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
        api_key = api_key or os.environ.get("XAGENT_API_KEY")
        base_url = base_url or os.environ.get("XAGENT_BASE_URL")
        if not api_key:
            raise ValueError("api_key required: pass api_key=... or set XAGENT_API_KEY")
        if not base_url:
            raise ValueError(
                "base_url required: pass base_url=... or set XAGENT_BASE_URL"
            )

        self._http = HTTPClient(
            base_url=base_url,
            api_key=api_key,
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

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
    ) -> httpx.Response:
        """Send a request and map any 4xx/5xx response to an XAgentError.

        Protected by package convention: called by ``TasksAPI`` within
        ``xagent_sdk`` but not part of the user-facing API. The single
        underscore signals "internal to the package" not "internal to
        this class".

        Transport-level failures are already wrapped in
        ``XAgentTransportError`` by ``HTTPClient.request``; this helper
        only adds the V1-envelope-to-exception mapping for HTTP error
        responses that do have a body.
        """
        resp = self._http.request(method, path, json=json)
        if resp.is_error:
            raise from_response(resp)
        return resp
