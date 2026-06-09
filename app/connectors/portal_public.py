"""
Cookie-less HTTPS client for the small slice of portal.example.com that is
publicly readable.

Most portal endpoints require an authenticated Okta session, but a few
return JSON without any cookie or token. The only one we currently rely on
is :data:`SECURITY_ADVISORIES_PATH` — confirmed reachable with a 200 from a
fresh client. Adding more public endpoints here is safe: the connector will
simply 403 and the caller will fall through to the cached/stale path.

Why this exists: the InsightsWorker can keep producing advisory matches
against Salesforce-derived cluster fingerprints even when the headless
browser bridge is down or the daemon never ran. That removes Okta SSO from
the critical path for the INSIGHTS card.

This module does NO retry/backoff of its own — the caller (the globals
sync worker) wraps it with the usual ``BaseWorker`` retry-with-heal flow.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

PORTAL_BASE = "https://portal.example.com"
SECURITY_ADVISORIES_PATH = "/api/v1/pages/securityAdvisories"

DEFAULT_TIMEOUT = 15.0

# Mimic a normal browser request so any future WAF rule that drops bare
# python-httpx UA strings doesn't silently flip our 200s to 403s.
_BROWSER_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": f"{PORTAL_BASE}/",
}


class PortalPublicError(RuntimeError):
    """Raised when a public endpoint returns a non-2xx response."""

    def __init__(self, path: str, status: int, body_excerpt: str = "") -> None:
        super().__init__(
            f"portal.example.com {path} returned HTTP {status}"
            + (f": {body_excerpt[:200]}" if body_excerpt else "")
        )
        self.path = path
        self.status = status


async def _get_json(
    path: str, *, timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    url = PORTAL_BASE + path
    async with httpx.AsyncClient(
        timeout=timeout, headers=_BROWSER_HEADERS, follow_redirects=True,
    ) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        raise PortalPublicError(path, resp.status_code, resp.text or "")
    ct = resp.headers.get("content-type", "")
    if "json" not in ct.lower():
        raise PortalPublicError(
            path, resp.status_code, f"non-json content-type {ct!r}"
        )
    return resp.json()


async def fetch_security_advisories() -> list[dict[str, Any]]:
    """Return the list of Nutanix security advisories.

    The public endpoint returns ``{"id": ..., "content": {"body": [...]}}``.
    The ``body`` array carries dicts with ``advisoryNumber``, ``title``,
    ``date``, ``affectedVersions``, ``summary``, and ``href``. We unwrap to
    the list because that's what ``_match_advisories`` already expects.
    """
    raw = await _get_json(SECURITY_ADVISORIES_PATH)
    if not isinstance(raw, dict):
        raise PortalPublicError(
            SECURITY_ADVISORIES_PATH, 200, f"unexpected top-level type {type(raw).__name__}"
        )
    content = raw.get("content")
    body: Any = []
    if isinstance(content, dict):
        body = content.get("body", [])
    if not isinstance(body, list):
        raise PortalPublicError(
            SECURITY_ADVISORIES_PATH, 200,
            f"content.body is not a list ({type(body).__name__})",
        )
    return [item for item in body if isinstance(item, dict)]


async def probe_endpoint(path: str) -> Optional[int]:
    """Return the HTTP status of ``path`` (used for diagnostics).

    ``None`` if the request fails before the server can answer (e.g. DNS
    or TLS failure). Useful for the sync-observability page if we ever
    want to show "tried EOL endpoint, got 403" for the operator.
    """
    url = PORTAL_BASE + path
    try:
        async with httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT, headers=_BROWSER_HEADERS,
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
        return resp.status_code
    except httpx.HTTPError as exc:
        logger.debug("portal_public probe %s failed: %s", path, exc)
        return None
