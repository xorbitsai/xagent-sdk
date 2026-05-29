"""Internal base class shared by every public SDK client.

The two public clients (``XAgentClient`` / ``AgentClient`` for runtime, and
the upcoming ``UserClient`` for management) only differ in which env var
provides the API key fallback and which surface methods they expose.
Everything else -- env resolution, ``HTTPClient`` ownership, the
4xx/5xx-to-exception mapping in ``_request``, ``close()``, and the
context-manager protocol -- is identical, so it lives here.

Subclasses customize the key resolution by overriding two class
attributes:

- ``_ENV_API_KEY``: the environment variable consulted when the caller
  does not pass an explicit key (``XAGENT_API_KEY`` for the runtime
  client, ``XAGENT_PERSONAL_KEY`` for the user client).
- ``_API_KEY_FIELD``: the parameter name to use in the ``ValueError``
  message when the key is missing. Showing the right name keeps the
  error actionable for whichever public surface raised it.
"""

import os
from types import TracebackType
from typing import ClassVar, Self

import httpx

from xagent_sdk._http import HTTPClient
from xagent_sdk.errors import from_response


class _BaseClient:
    """Shared transport plumbing for SDK clients.

    Not part of the public surface; subclasses are.
    """

    _ENV_API_KEY: ClassVar[str] = "XAGENT_API_KEY"
    _API_KEY_FIELD: ClassVar[str] = "api_key"

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
        api_key = api_key or os.environ.get(self._ENV_API_KEY)
        base_url = base_url or os.environ.get("XAGENT_BASE_URL")
        if not api_key:
            raise ValueError(
                f"{self._API_KEY_FIELD} required: "
                f"pass {self._API_KEY_FIELD}=... or set {self._ENV_API_KEY}"
            )
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

        Protected by package convention: called by API namespace classes
        within ``xagent_sdk`` (TasksAPI, TemplatesAPI, AgentsAPI) but not
        part of the user-facing API.

        Transport-level failures are already wrapped in
        ``XAgentTransportError`` by ``HTTPClient.request``; this helper
        only adds the V1-envelope-to-exception mapping for HTTP error
        responses that do have a body.
        """
        resp = self._http.request(method, path, json=json)
        if resp.is_error:
            raise from_response(resp)
        return resp
