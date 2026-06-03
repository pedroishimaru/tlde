"""Fetch discovered artifacts with content-hash caching and paywall detection.

Caches by URL under ``<cache_dir>/web/`` so re-runs don't re-download, records a
content hash for provenance, and refuses paywalled/login-walled responses rather
than ingesting a sign-in page as if it were a datasheet.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from tlde.ingest.cache import hash_bytes

_LOGIN_MARKERS = (
    "sign in", "log in", "login", "create an account", "subscribe to download",
    "please register", "access denied", "paywall", "captcha",
)


def looks_like_wall(status: int, content_type: str, body: bytes) -> str | None:
    """Return a reason string if the response looks like a paywall/login wall."""
    if status in (401, 402, 403):
        return f"HTTP {status} (auth/paywall)"
    ct = (content_type or "").lower()
    if "text/html" in ct:
        sample = body[:4000].decode("utf-8", "ignore").lower()
        if any(m in sample for m in _LOGIN_MARKERS):
            return "login/paywall markers in HTML"
    return None


def _dest_name(url: str) -> str:
    base = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    name = base.rsplit("/", 1)[-1] or "download"
    digest = hashlib.sha256(url.encode()).hexdigest()[:8]
    return f"{digest}-{name}"


async def fetch(url: str, cache_dir: str, client=None) -> tuple[str | None, str, str]:
    """Fetch ``url`` into ``<cache_dir>/web/``.

    Returns (path|None, content_hash, reason). ``client`` is an injectable async
    HTTP client (for tests); defaults to httpx.
    """
    web = Path(cache_dir) / "web"
    web.mkdir(parents=True, exist_ok=True)
    dest = web / _dest_name(url)
    if dest.is_file():
        data = dest.read_bytes()
        return str(dest), hash_bytes(data), "cache hit"

    owns = client is None
    if owns:
        import httpx
        client = httpx.AsyncClient(follow_redirects=True, timeout=60)
    try:
        resp = await client.get(url)
        status = resp.status_code
        ctype = resp.headers.get("content-type", "")
        body = resp.content
    except Exception as e:
        return None, "", f"fetch error: {type(e).__name__}: {e}"
    finally:
        if owns:
            await client.aclose()

    if status >= 400:
        return None, "", f"HTTP {status}"
    wall = looks_like_wall(status, ctype, body)
    if wall:
        return None, "", wall
    if not body:
        return None, "", "empty body"

    dest.write_bytes(body)
    return str(dest), hash_bytes(body), "fetched"
