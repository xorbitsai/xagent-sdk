"""Internal base class shared by every public SDK client.

The two public clients (``AgentClient`` for runtime chat tasks and
``UserClient`` for management endpoints) only differ in which env var
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
- ``_DEFAULT_BASE_URL``: a hosted endpoint to fall back to when neither
  an explicit ``base_url`` nor ``XAGENT_BASE_URL`` is supplied. ``None``
  means there is no default and a base URL must be provided (the
  self-hosted clients); a subclass talking to a fixed hosted service
  sets it to that URL.
"""

import os
from types import TracebackType
from typing import ClassVar, Self

import httpx

from xagent_sdk._http import HTTPClient
from xagent_sdk.errors import from_response


def _resolve(
    explicit: str | None, env_name: str, default: str | None = None
) -> str | None:
    """Resolve a config value: explicit argument, then env var, then default.

    Only a *genuinely absent* value (``None``) falls through to the next
    source. A value that was provided but empty -- an explicit ``""`` or an
    env var set to ``""`` -- is returned as-is so the caller's
    ``if not value`` guard fails fast, rather than being swallowed (as a
    falsy ``or`` would) and silently replaced by the env var or the hosted
    default. This is the single resolution path for every credential/URL so
    the empty-vs-absent distinction cannot be re-derived inconsistently.
    """
    if explicit is not None:
        return explicit
    env = os.environ.get(env_name)
    if env is not None:
        return env
    return default


class _BaseClient:
    """Shared transport plumbing for SDK clients.

    Not part of the public surface; subclasses are.
    """

    _ENV_API_KEY: ClassVar[str] = "XAGENT_API_KEY"
    _API_KEY_FIELD: ClassVar[str] = "api_key"
    _DEFAULT_BASE_URL: ClassVar[str | None] = None
    # How a subclass tells the caller to supply a base URL when none could
    # be resolved. Overridden where the public way to set it differs (e.g.
    # the workspace client's ``region=``).
    _BASE_URL_HINT: ClassVar[str] = "pass base_url=... or set XAGENT_BASE_URL"

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
        # Resolve through the shared helper so an explicitly empty key/URL
        # (or an env var set to "") fails fast instead of being swapped for
        # the env value or the hosted default.
        api_key = _resolve(api_key, self._ENV_API_KEY)
        base_url = _resolve(base_url, "XAGENT_BASE_URL", self._DEFAULT_BASE_URL)
        if not api_key:
            raise ValueError(
                f"{self._API_KEY_FIELD} required: "
                f"pass {self._API_KEY_FIELD}=... or set {self._ENV_API_KEY}"
            )
        if not base_url:
            raise ValueError(f"base_url required: {self._BASE_URL_HINT}")

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
