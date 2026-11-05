"""Bounded HTTP(S) downloads for media the agent sends by URL (send_image, send_multiple_images).

httpx is a Hermes base dependency; it is imported lazily so the rest of h3proto works without it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional, Tuple

TIMEOUT = 20.0          # seconds, per network operation
TOTAL = 60.0            # seconds, whole download
MAX_BYTES = 25 * 1024 * 1024
MAX_REDIRECTS = 5
USER_AGENT = "Mozilla/5.0 (Hermes3DS)"


class DownloadError(Exception):
    """The URL could not be fetched; the message is short and safe to show."""


async def download(url: str, *, max_bytes: int = MAX_BYTES, timeout: float = TIMEOUT, total: float = TOTAL,
                   is_safe: Optional[Callable[[str], Awaitable[bool]]] = None,
                   transport: Any = None) -> Tuple[bytes, str, str]:
    """GET `url`: http(s) only, `is_safe` consulted for the URL and every redirect target, at most
    `max_bytes` (checked against Content-Length and while streaming, after content decoding).

    Returns (body, mime without parameters, lowercased; final URL path). Raises DownloadError.
    `transport` is for tests (httpx.MockTransport).
    """
    import httpx

    if not url.lower().startswith(("http://", "https://")):
        raise DownloadError("not an http(s) URL")

    async def check(request: "httpx.Request") -> None:
        if request.url.scheme not in ("http", "https"):
            raise DownloadError(f"redirect to a non-http(s) URL ({request.url.scheme})")
        if is_safe is not None and not await is_safe(str(request.url)):
            raise DownloadError("blocked URL (private or unsafe address)")

    async def fetch() -> Tuple[bytes, str, str]:
        opts = dict(timeout=timeout, follow_redirects=True, max_redirects=MAX_REDIRECTS,
                    event_hooks={"request": [check]}, headers={"User-Agent": USER_AGENT})
        client = None
        if transport is None:
            # Inside Hermes: its client checks every address at connect time, so a name that passed `is_safe`
            # can't be re-resolved to a private one (DNS rebinding), and it ignores no redirect.
            try:
                from gateway.platforms.helpers import platform_http_client
                client = platform_http_client(**opts)
            except ImportError:
                pass
        if client is None:
            client = httpx.AsyncClient(transport=transport, **opts)
        async with client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise DownloadError(f"HTTP {resp.status_code}")
                try:
                    announced = int(resp.headers.get("content-length") or 0)
                except ValueError:
                    announced = 0
                if announced > max_bytes:
                    raise DownloadError("file too large")
                buf = bytearray()
                async for part in resp.aiter_bytes():
                    buf += part
                    if len(buf) > max_bytes:
                        raise DownloadError("file too large")
                mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                return bytes(buf), mime, str(resp.url.path)

    try:
        return await asyncio.wait_for(fetch(), total)
    except DownloadError:
        raise
    except (asyncio.TimeoutError, httpx.TimeoutException):
        raise DownloadError("timed out") from None
    except httpx.TooManyRedirects:
        raise DownloadError("too many redirects") from None
    except httpx.UnsupportedProtocol:
        raise DownloadError("redirect to a non-http(s) URL") from None
    except httpx.HTTPError as exc:
        raise DownloadError(f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__) from None
