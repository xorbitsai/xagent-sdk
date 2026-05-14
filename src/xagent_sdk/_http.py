from types import TracebackType
from typing import Any, Self

import httpx

from xagent_sdk._version import __version__

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_CONNECT_TIMEOUT = 10.0
_DEFAULT_MAX_CONNECTIONS = 10


def _build_user_agent() -> str:
    return f"xagent-sdk-python/{__version__} (httpx/{httpx.__version__})"


class HTTPClient:
    """Thin httpx.Client wrapper for the xAgent v1 API.

    Owns a single httpx.Client (connection pool) for the lifetime of the
    enclosing XAgentClient. Returns raw httpx.Response objects;
    HTTP-status-to-exception mapping is layered on top in a later commit.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        max_connections: int = _DEFAULT_MAX_CONNECTIONS,
        user_agent: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must be a non-empty string")
        if not base_url:
            raise ValueError("base_url must be a non-empty string")

        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": user_agent or _build_user_agent(),
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(timeout, connect=_DEFAULT_CONNECT_TIMEOUT),
            limits=httpx.Limits(max_connections=max_connections),
            transport=transport,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        return self._client.request(method, path, json=json)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()
