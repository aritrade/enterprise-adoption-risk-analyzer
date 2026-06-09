"""
Headless browser bridge for authenticated Nutanix portal access.

Connects to a long-running headless Chromium daemon via Chrome DevTools
Protocol (CDP).  The daemon keeps all session cookies in memory — no
cookie files on disk, no stored credentials.

Architecture:
  1. ``scripts/browser_daemon.py --bg`` launches a headless Chromium
     process with remote debugging enabled.
  2. ``scripts/browser_setup.py`` opens a headed browser for the user
     to complete Okta SSO, then transfers cookies to the daemon via CDP.
  3. This bridge connects to the daemon, creates dedicated pages for
     portal and CS Insights, and makes API calls through the browser's
     native cookie jar.
  4. A periodic keep-alive task navigates to each portal to prevent
     session timeouts (runs every 2 hours).
  5. If a session expires mid-flight, the bridge raises PermissionError
     so the aggregator can fall back to demo data and alert the user.

No cookies on disk.  No credentials stored.  As long as the daemon
runs, authenticated sessions persist.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

CDP_URL = "http://localhost:9223"

PORTAL_HOME = "https://portal.example.com/page/home"
PORTAL_API_BASE = "https://portal.example.com/api/v1"

CSINSIGHTS_HOME = "https://csinsights.example.com"
CSINSIGHTS_API_BASE = "https://csinsights.internal.example"


def _derive_planhat_api_base(home_url: str) -> str:
    """Default API base = scheme://host + /api (drops any path/workspace slug)."""
    parsed = urllib.parse.urlsplit(home_url or "https://app.planhat.example/your-tenant")
    host = parsed.netloc or "app.planhat.example"
    return f"{parsed.scheme or 'https'}://{host}/api"


# Planhat URLs are workspace-specific. The Nutanix tenant lives at
# https://app.planhat.example/your-tenant (SPA home) and serves its cookie-
# authenticated API at https://app.planhat.example/api/*. Both URLs are
# settings-driven so other tenants can point this at app.planhat.com,
# eu.planhat.com, etc. without code changes.
PLANHAT_HOME = settings.planhat_base_url or "https://app.planhat.example/your-tenant"
PLANHAT_API_BASE = settings.planhat_api_base_url or _derive_planhat_api_base(
    PLANHAT_HOME
)

AUTH_TIMEOUT_SECONDS = 60
AUTH_POLL_INTERVAL = 1.5

KEEP_ALIVE_INTERVAL_SECONDS = 2 * 60 * 60  # 2 hours

_JS_FETCH = """
async ([url, options]) => {
    try {
        const resp = await fetch(url, {
            ...options,
            credentials: 'include',
        });
        const ct = resp.headers.get('content-type') || '';
        if (resp.ok && ct.includes('json')) {
            return { ok: true, status: resp.status, data: await resp.json() };
        }
        const text = await resp.text().catch(() => '');
        return { ok: false, status: resp.status, body: text.substring(0, 500) };
    } catch(e) {
        return { ok: false, status: 0, body: e.message };
    }
}
"""


def _build_url(base: str, path: str, params: Optional[dict] = None) -> str:
    url = base + path
    if not params:
        return url
    parts: List[str] = []
    for k, v in params.items():
        if isinstance(v, list):
            for item in v:
                parts.append(f"{k}={urllib.parse.quote(str(item))}")
        else:
            parts.append(f"{k}={urllib.parse.quote(str(v))}")
    return url + "?" + "&".join(parts)


def _is_sso_url(url: str) -> bool:
    lower = url.lower()
    return any(
        frag in lower
        for frag in ("okta.com", "/idp/", "/sso/", "/saml/", "login.microsoftonline")
    )


class PlaywrightBridge:
    """Connects to the headless Chromium daemon via CDP for portal access."""

    def __init__(self) -> None:
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._portal_page: Any = None
        self._cs_page: Any = None
        self._planhat_page: Any = None
        self._started = False
        self._portal_authed = False
        self._cs_authed = False
        self._planhat_authed = False
        self._start_lock = asyncio.Lock()
        self._portal_lock = asyncio.Lock()
        self._cs_lock = asyncio.Lock()
        self._planhat_lock = asyncio.Lock()
        self._keep_alive_task: Optional[asyncio.Task] = None

    @property
    def portal_authenticated(self) -> bool:
        return self._portal_authed

    @property
    def csinsights_authenticated(self) -> bool:
        return self._cs_authed

    @property
    def planhat_authenticated(self) -> bool:
        return self._planhat_authed


    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect to the running headless Chromium daemon via CDP."""
        async with self._start_lock:
            if self._started:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                raise ImportError(
                    "Playwright is required. "
                    "Install: pip install playwright && playwright install chromium"
                )

            self._pw = await async_playwright().start()
            try:
                self._browser = await self._pw.chromium.connect_over_cdp(CDP_URL)
            except Exception as exc:
                await self._pw.stop()
                self._pw = None
                raise RuntimeError(
                    f"Cannot connect to browser daemon at {CDP_URL}: {exc}\n"
                    "Start the daemon first: python scripts/browser_daemon.py --bg\n"
                    "Then run setup:          python scripts/browser_setup.py"
                ) from exc

            # Use the daemon's default browser context (shares cookies)
            contexts = self._browser.contexts
            self._context = contexts[0] if contexts else await self._browser.new_context()

            self._started = True
            logger.info("PlaywrightBridge connected to daemon at %s", CDP_URL)

            # Start the keep-alive background task
            self._keep_alive_task = asyncio.create_task(self._keep_alive_loop())

    async def stop(self) -> None:
        """Disconnect from the daemon (does NOT stop the daemon itself)."""
        if self._keep_alive_task:
            self._keep_alive_task.cancel()
            try:
                await self._keep_alive_task
            except asyncio.CancelledError:
                pass
            self._keep_alive_task = None

        for page in (self._portal_page, self._cs_page, self._planhat_page):
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
        self._portal_page = None
        self._cs_page = None
        self._planhat_page = None

        # Don't close the context or browser — they belong to the daemon
        self._context = None
        self._browser = None

        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

        self._started = False
        self._portal_authed = False
        self._cs_authed = False
        self._planhat_authed = False
        logger.info("PlaywrightBridge disconnected (daemon still running)")

    async def _ensure_started(self) -> None:
        if not self._started:
            await self.start()

    # ── Keep-alive ────────────────────────────────────────────────────

    async def _keep_alive_loop(self) -> None:
        """Periodically navigate to portals to prevent session timeouts."""
        while True:
            await asyncio.sleep(KEEP_ALIVE_INTERVAL_SECONDS)
            try:
                await self._do_keep_alive()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Keep-alive error: %s", exc)

    async def _do_keep_alive(self) -> None:
        logger.info("Running session keep-alive…")

        if self._portal_authed and self._portal_page:
            try:
                await self._portal_page.goto(
                    PORTAL_HOME, wait_until="domcontentloaded", timeout=20000,
                )
                await asyncio.sleep(2)
                if await self._check_portal_auth():
                    logger.info("Keep-alive: portal session OK")
                else:
                    logger.warning("Keep-alive: portal session expired")
                    self._portal_authed = False
            except Exception as exc:
                logger.warning("Keep-alive portal error: %s", exc)

        if self._cs_authed and self._cs_page:
            try:
                await self._cs_page.goto(
                    CSINSIGHTS_HOME, wait_until="domcontentloaded", timeout=20000,
                )
                await asyncio.sleep(2)
                if await self._check_cs_auth():
                    logger.info("Keep-alive: CS Insights session OK")
                else:
                    logger.warning("Keep-alive: CS Insights session expired")
                    self._cs_authed = False
            except Exception as exc:
                logger.warning("Keep-alive CS error: %s", exc)

        if self._planhat_authed and self._planhat_page:
            try:
                await self._planhat_page.goto(
                    PLANHAT_HOME, wait_until="domcontentloaded", timeout=20000,
                )
                await asyncio.sleep(2)
                if await self._check_planhat_auth():
                    logger.info("Keep-alive: Planhat session OK")
                else:
                    logger.warning("Keep-alive: Planhat session expired")
                    self._planhat_authed = False
            except Exception as exc:
                logger.warning("Keep-alive Planhat error: %s", exc)

    # ── Portal authentication ─────────────────────────────────────────

    async def ensure_portal_auth(self) -> bool:
        """Verify portal session is live (set up by the setup script)."""
        async with self._portal_lock:
            await self._ensure_started()

            if self._portal_page is None:
                self._portal_page = await self._context.new_page()

            if self._portal_authed:
                if await self._check_portal_auth():
                    return True
                self._portal_authed = False

            logger.info("Verifying portal.example.com session…")
            try:
                await self._portal_page.goto(
                    PORTAL_HOME, wait_until="domcontentloaded", timeout=30000,
                )
            except Exception as exc:
                logger.warning("Portal navigation: %s", exc)

            # Wait for SSO redirect to complete (Okta cookies in daemon)
            polls = int(AUTH_TIMEOUT_SECONDS / AUTH_POLL_INTERVAL)
            for i in range(polls):
                await asyncio.sleep(AUTH_POLL_INTERVAL)
                url = self._portal_page.url
                if "portal.example.com" in url and not _is_sso_url(url):
                    if await self._check_portal_auth():
                        self._portal_authed = True
                        logger.info("Portal authenticated successfully")
                        return True
                if i > 0 and i % 10 == 0:
                    logger.debug(
                        "Waiting for portal SSO… (%ds, url: %s)",
                        int(i * AUTH_POLL_INTERVAL), url[:80],
                    )

            logger.error(
                "Portal session not available. "
                "Run `python scripts/browser_setup.py` to authenticate."
            )
            return False

    async def _check_portal_auth(self) -> bool:
        try:
            result = await self._portal_page.evaluate("""
                async () => {
                    try {
                        const r = await fetch('/api/v1/auth', {credentials: 'include'});
                        if (!r.ok) return null;
                        const ct = r.headers.get('content-type') || '';
                        if (!ct.includes('json')) return null;
                        return await r.json();
                    } catch(e) { return null; }
                }
            """)
            if not result:
                return False
            user = result.get("user", {})
            name = user.get("firstName", "Guest")
            return name != "Guest" and "tools-eng" not in (user.get("email") or "")
        except Exception:
            return False

    # ── CS Insights authentication ────────────────────────────────────

    async def ensure_cs_auth(self) -> bool:
        """Verify CS Insights session is live (set up by the setup script)."""
        async with self._cs_lock:
            await self._ensure_started()

            if self._cs_page is None:
                self._cs_page = await self._context.new_page()

            if self._cs_authed:
                if await self._check_cs_auth():
                    return True
                self._cs_authed = False

            logger.info("Verifying csinsights.example.com session…")

            # First try navigating directly to the backend for auth check.
            # This avoids cross-origin fetch issues in the headless context.
            if await self._check_cs_auth_direct():
                self._cs_authed = True
                logger.info("CS Insights authenticated successfully (direct)")
                return True

            try:
                await self._cs_page.goto(
                    CSINSIGHTS_HOME, wait_until="domcontentloaded", timeout=20000,
                )
            except Exception as exc:
                logger.warning("CS Insights navigation: %s", exc)

            # Wait for SPA to fully load before cross-origin fetch
            for attempt in range(12):
                await asyncio.sleep(3)
                if await self._check_cs_auth():
                    self._cs_authed = True
                    logger.info("CS Insights authenticated successfully")
                    return True

            logger.error(
                "CS Insights session not available. "
                "Run `python scripts/browser_setup.py` to authenticate."
            )
            return False

    async def _check_cs_auth_direct(self) -> bool:
        """Navigate the page to the backend origin and fetch /user/me same-origin."""
        try:
            await self._cs_page.goto(
                CSINSIGHTS_API_BASE + "/user/me",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            await asyncio.sleep(1)
            content = await self._cs_page.evaluate(
                """() => {
                    try {
                        const pre = document.querySelector('pre');
                        const text = pre ? pre.innerText : document.body.innerText;
                        return JSON.parse(text);
                    } catch(e) { return null; }
                }"""
            )
            if content and isinstance(content, dict):
                logger.info("CS auth direct check: got user data")
                return True
            logger.info("CS auth direct check: no parseable user data")
            return False
        except Exception as exc:
            logger.info("CS auth direct check error: %s", exc)
            return False

    async def _check_cs_auth(self) -> bool:
        try:
            result = await self._cs_page.evaluate(
                """async (apiBase) => {
                    try {
                        const r = await fetch(apiBase + '/user/me', {
                            credentials: 'include',
                            headers: {'Accept': 'application/json'},
                        });
                        if (!r.ok) return {ok: false, reason: 'HTTP ' + r.status};
                        const ct = r.headers.get('content-type') || '';
                        if (!ct.includes('json')) return {ok: false, reason: 'not-json: ' + ct};
                        return {ok: true, data: await r.json()};
                    } catch(e) { return {ok: false, reason: e.message}; }
                }""",
                CSINSIGHTS_API_BASE,
            )
            if result and result.get("ok"):
                return True
            reason = (result or {}).get("reason", "unknown")
            logger.info("CS auth check: %s", reason)
            return False
        except Exception as exc:
            logger.info("CS auth check error: %s", exc)
            return False

    # ── Portal API methods ────────────────────────────────────────────

    async def portal_get(self, path: str, params: Optional[dict] = None) -> Any:
        await self._ensure_started()
        if not self._portal_authed:
            await self.ensure_portal_auth()
        url = _build_url(PORTAL_API_BASE, path, params)
        return await self._portal_fetch(url, {
            "method": "GET",
            "headers": {"Accept": "application/json"},
        })

    async def portal_post(self, path: str, payload: Optional[dict] = None) -> Any:
        await self._ensure_started()
        if not self._portal_authed:
            await self.ensure_portal_auth()
        url = _build_url(PORTAL_API_BASE, path)
        return await self._portal_fetch(url, {
            "method": "POST",
            "headers": {"Accept": "application/json", "Content-Type": "application/json"},
            "body": json.dumps(payload or {}),
        })

    async def _portal_fetch(self, url: str, options: dict, *, _retry: bool = True) -> Any:
        await self._ensure_page_on_domain(self._portal_page, "portal.example.com", PORTAL_HOME)
        result = await self._portal_page.evaluate(_JS_FETCH, [url, options])

        if result.get("ok"):
            return result["data"]

        status = result.get("status", 0)
        if status in (401, 403) and _retry:
            logger.warning("Portal session expired (HTTP %d), re-verifying…", status)
            self._portal_authed = False
            if await self.ensure_portal_auth():
                return await self._portal_fetch(url, options, _retry=False)

        if status in (401, 403):
            raise PermissionError(
                f"Portal auth failed (HTTP {status}). "
                "Run `python scripts/browser_setup.py` to re-authenticate."
            )
        raise RuntimeError(f"Portal API error: HTTP {status} — {result.get('body', '')[:200]}")

    # ── CS Insights API methods ───────────────────────────────────────

    async def cs_get(self, path: str, params: Optional[dict] = None) -> Any:
        await self._ensure_started()
        if not self._cs_authed:
            await self.ensure_cs_auth()
        url = _build_url(CSINSIGHTS_API_BASE, path, params)
        return await self._cs_fetch(url, {
            "method": "GET",
            "headers": {"Accept": "application/json"},
        })

    async def cs_post(
        self, path: str, payload: dict, headers: Optional[dict] = None
    ) -> Any:
        await self._ensure_started()
        if not self._cs_authed:
            await self.ensure_cs_auth()
        url = _build_url(CSINSIGHTS_API_BASE, path)
        fetch_headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if headers:
            fetch_headers.update(headers)
        return await self._cs_fetch(url, {
            "method": "POST",
            "headers": fetch_headers,
            "body": json.dumps(payload),
        })

    async def _cs_fetch(self, url: str, options: dict, *, _retry: bool = True) -> Any:
        await self._ensure_page_on_domain(self._cs_page, "csinsights", CSINSIGHTS_HOME)
        result = await self._cs_page.evaluate(_JS_FETCH, [url, options])

        if result.get("ok"):
            return result["data"]

        status = result.get("status", 0)
        if status in (401, 403) and _retry:
            logger.warning("CS Insights session expired (HTTP %d), re-verifying…", status)
            self._cs_authed = False
            if await self.ensure_cs_auth():
                return await self._cs_fetch(url, options, _retry=False)

        if status in (401, 403):
            raise PermissionError(
                f"CS Insights auth failed (HTTP {status}). "
                "Run `python scripts/browser_setup.py` to re-authenticate."
            )
        raise RuntimeError(f"CS Insights API error: HTTP {status} — {result.get('body', '')[:200]}")

    # ── Planhat authentication ────────────────────────────────────────

    async def ensure_planhat_auth(self) -> bool:
        """Verify Planhat session is live (set up by ``scripts/planhat_login.py``)."""
        async with self._planhat_lock:
            await self._ensure_started()

            if self._planhat_page is None:
                self._planhat_page = await self._context.new_page()

            if self._planhat_authed:
                if await self._check_planhat_auth():
                    return True
                self._planhat_authed = False

            logger.info("Verifying Planhat session at %s …", PLANHAT_HOME)
            try:
                await self._planhat_page.goto(
                    PLANHAT_HOME, wait_until="domcontentloaded", timeout=30000,
                )
            except Exception as exc:
                logger.warning("Planhat navigation: %s", exc)

            # Planhat's own login flow is its primary auth path; some tenants
            # also wire it to Okta SSO. Either way we just wait until the
            # browser settles on an app.planhat.com URL that is not the
            # login/SSO redirect.
            polls = int(AUTH_TIMEOUT_SECONDS / AUTH_POLL_INTERVAL)
            for i in range(polls):
                await asyncio.sleep(AUTH_POLL_INTERVAL)
                url = self._planhat_page.url
                lower = url.lower()
                if (
                    "planhat.com" in lower
                    and "login" not in lower
                    and "signin" not in lower
                    and not _is_sso_url(url)
                ):
                    if await self._check_planhat_auth():
                        self._planhat_authed = True
                        logger.info("Planhat authenticated successfully")
                        return True
                if i > 0 and i % 10 == 0:
                    logger.debug(
                        "Waiting for Planhat session… (%ds, url: %s)",
                        int(i * AUTH_POLL_INTERVAL), url[:80],
                    )

            logger.error(
                "Planhat session not available. "
                "Run `python scripts/planhat_login.py` to authenticate."
            )
            return False

    async def _check_planhat_auth(self) -> bool:
        """Confirm we have a cookie-bound Planhat session.

        Planhat's per-tenant SPA paths vary (some tenants live under
        ``/api/v1/...``, others under ``/api/...``, others workspace-
        prefixed). Rather than guess, we trust the URL signal first:
        if the daemon's page settled on a planhat workspace URL after
        navigating to ``PLANHAT_HOME`` and is NOT on a login/SSO/SAML
        redirect, the cookie jar is good enough — the connector's own
        company/health/nps queries will surface any real auth errors
        as PermissionError downstream.

        Best-effort JSON probe against a handful of common endpoints
        runs second; a positive answer there is a stronger signal we
        log at INFO, but a negative answer no longer disables the
        connector.
        """
        try:
            url = self._planhat_page.url
        except Exception:
            return False

        lower = url.lower()
        url_signed_in = (
            "planhat.com" in lower
            and "login" not in lower
            and "signin" not in lower
            and not _is_sso_url(url)
        )

        if not url_signed_in:
            return False

        probes = (
            "/api/users/me",
            "/api/me",
            "/api/session",
            "/api/v1/users/me",
            "/api/v1/me",
            "/api/auth/me",
        )
        for path in probes:
            try:
                result = await self._planhat_page.evaluate(
                    """async (path) => {
                        try {
                            const r = await fetch(path, {
                                credentials: 'include',
                                headers: {'Accept': 'application/json'},
                            });
                            if (!r.ok) return {ok: false, status: r.status};
                            const ct = r.headers.get('content-type') || '';
                            if (!ct.includes('json')) return {ok: false, status: 0, reason: 'not-json'};
                            return {ok: true, data: await r.json()};
                        } catch(e) { return {ok: false, status: 0, reason: e.message}; }
                    }""",
                    path,
                )
                if result and result.get("ok"):
                    logger.info("Planhat auth probe matched: %s", path)
                    return True
            except Exception as exc:
                logger.debug("Planhat auth probe %s error: %s", path, exc)

        # No probe matched, but the URL signal says we're inside the
        # workspace. Trust it — the connector's actual queries are the
        # real source of truth for whether the session is good.
        logger.info(
            "Planhat auth confirmed via URL signal (%s) — no auth-probe "
            "endpoint matched; will treat session as live and let the "
            "connector's own queries verify.", url[:80],
        )
        return True

    # ── Planhat API methods ───────────────────────────────────────────

    async def planhat_get(self, path: str, params: Optional[dict] = None) -> Any:
        await self._ensure_started()
        if not self._planhat_authed:
            await self.ensure_planhat_auth()
        url = _build_url(PLANHAT_API_BASE, path, params)
        return await self._planhat_fetch(url, {
            "method": "GET",
            "headers": {"Accept": "application/json"},
        })

    async def planhat_post(
        self, path: str, payload: dict, headers: Optional[dict] = None
    ) -> Any:
        await self._ensure_started()
        if not self._planhat_authed:
            await self.ensure_planhat_auth()
        url = _build_url(PLANHAT_API_BASE, path)
        fetch_headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if headers:
            fetch_headers.update(headers)
        return await self._planhat_fetch(url, {
            "method": "POST",
            "headers": fetch_headers,
            "body": json.dumps(payload),
        })

    async def _planhat_fetch(self, url: str, options: dict, *, _retry: bool = True) -> Any:
        await self._ensure_page_on_domain(
            self._planhat_page, "planhat.com", PLANHAT_HOME,
        )
        result = await self._planhat_page.evaluate(_JS_FETCH, [url, options])

        if result.get("ok"):
            return result["data"]

        status = result.get("status", 0)
        if status in (401, 403) and _retry:
            logger.warning("Planhat session expired (HTTP %d), re-verifying…", status)
            self._planhat_authed = False
            if await self.ensure_planhat_auth():
                return await self._planhat_fetch(url, options, _retry=False)

        if status in (401, 403):
            raise PermissionError(
                f"Planhat auth failed (HTTP {status}). "
                "Run `python scripts/planhat_login.py` to re-authenticate."
            )
        raise RuntimeError(
            f"Planhat API error: HTTP {status} — {result.get('body', '')[:200]}"
        )

    # ── CS Insights SPA scraping ─────────────────────────────────────

    async def cs_scrape_account_metrics(self, account_id: str) -> Optional[Dict[str, Any]]:
        """
        Navigate to the CS Insights SPA account detail page and extract
        rendered health/engagement/NPS metrics.

        Returns a dict with available metrics or None if scraping fails.
        The SPA computes these client-side — the REST API does not expose them.
        Only attempts scraping if already authenticated (does not re-auth).
        """
        if not self._started or not self._cs_authed:
            return None

        page = self._cs_page
        if page is None:
            return None

        captured_responses: List[Dict[str, Any]] = []

        async def _capture(response: Any) -> None:
            url = response.url
            if "csinsights-backend" not in url:
                return
            if response.status != 200:
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct:
                    body = await response.json()
                    captured_responses.append({"url": url, "data": body})
            except Exception:
                pass

        page.on("response", _capture)
        try:
            account_url = f"{CSINSIGHTS_HOME}/#/accounts/{account_id}"
            await page.goto(account_url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(5)

            metrics = await page.evaluate("""() => {
                const result = {};

                // Strategy 1: look for common metric patterns in the page
                const allText = document.body.innerText || '';

                // Health score - look for "Health Score" label near a number
                const healthMatch = allText.match(/Health\\s*Score[:\\s]*([\\d.]+)/i);
                if (healthMatch) result.health_score = parseFloat(healthMatch[1]);

                // Engagement score
                const engMatch = allText.match(/Engagement\\s*(?:Score)?[:\\s]*([\\d.]+)/i);
                if (engMatch) result.engagement_score = parseFloat(engMatch[1]);

                // NPS
                const npsMatch = allText.match(/NPS[:\\s]*([\\-\\d.]+)/i);
                if (npsMatch) result.nps_score = parseFloat(npsMatch[1]);

                // Adoption
                const adoptMatch = allText.match(/(?:Overall\\s*)?Adoption[:\\s]*([\\d.]+)\\s*%/i);
                if (adoptMatch) result.adoption_score = parseFloat(adoptMatch[1]);

                // Strategy 2: look for score-like elements (common SPA patterns)
                const scoreDivs = document.querySelectorAll(
                    '[class*="score"], [class*="health"], [class*="metric"], ' +
                    '[class*="gauge"], [class*="kpi"], [class*="stat"]'
                );
                for (const el of scoreDivs) {
                    const label = (el.getAttribute('aria-label') || el.getAttribute('title') || '').toLowerCase();
                    const text = (el.innerText || '').trim();
                    const numMatch = text.match(/^([\\-\\d.]+)/);
                    if (!numMatch) continue;
                    const val = parseFloat(numMatch[1]);
                    if (isNaN(val)) continue;

                    if (label.includes('health') && !result.health_score)
                        result.health_score = val;
                    else if (label.includes('engagement') && !result.engagement_score)
                        result.engagement_score = val;
                    else if (label.includes('nps') && !result.nps_score)
                        result.nps_score = val;
                    else if (label.includes('adoption') && !result.adoption_score)
                        result.adoption_score = val;
                }

                // Strategy 3: look for Angular/React rendered charts/gauges
                const canvases = document.querySelectorAll('canvas, svg');
                result._chart_count = canvases.length;

                result._url = window.location.href;
                result._has_data = Object.keys(result).filter(
                    k => !k.startsWith('_')
                ).length > 0;

                return result;
            }""")

            # Also check captured API responses for structured metric data
            for resp in captured_responses:
                url_lower = resp["url"].lower()
                data = resp.get("data", {})
                if isinstance(data, dict):
                    for key in ("healthScore", "health_score", "engagementScore",
                                "npsScore", "nps_score", "adoptionScore"):
                        val = data.get(key)
                        if val is not None and isinstance(val, (int, float)):
                            norm_key = key.replace("Score", "_score")
                            norm_key = norm_key if "_" in norm_key else key
                            if norm_key == "healthScore":
                                norm_key = "health_score"
                            elif norm_key == "engagementScore":
                                norm_key = "engagement_score"
                            elif norm_key == "npsScore":
                                norm_key = "nps_score"
                            elif norm_key == "adoptionScore":
                                norm_key = "adoption_score"
                            if norm_key not in metrics or metrics[norm_key] is None:
                                metrics[norm_key] = val

            if metrics.get("_has_data"):
                logger.info(
                    "SPA scrape for %s: health=%s engagement=%s nps=%s",
                    account_id,
                    metrics.get("health_score"),
                    metrics.get("engagement_score"),
                    metrics.get("nps_score"),
                )
                return {
                    k: v for k, v in metrics.items() if not k.startswith("_")
                } or None

            logger.info("SPA scrape for %s yielded no metrics (page: %s)", account_id, metrics.get("_url", "?"))
            return None

        except Exception as exc:
            logger.warning("SPA scrape failed for %s: %s", account_id, exc)
            return None
        finally:
            try:
                page.remove_listener("response", _capture)
            except Exception:
                pass

    # ── Portal Assets page scraping ──────────────────────────────────

    async def portal_scrape_account_assets(self, account_id: str) -> Optional[Dict[str, Any]]:
        """Deprecated — cluster/node data now comes from Salesforce Cluster__c.

        The portal's Pulse telemetry API (/groups/objects with
        ks_views.cluster_2) requires a 'View as Customer' session context
        that cannot be established programmatically.  Infrastructure data
        is instead sourced from the Salesforce Cluster__c custom object,
        which is directly queryable and account-scoped.
        """
        return None

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    async def _ensure_page_on_domain(page: Any, domain_fragment: str, fallback_url: str) -> None:
        """Re-navigate if the page has drifted away from the expected domain."""
        if page is None:
            return
        try:
            current = page.url
            if domain_fragment not in current:
                await page.goto(fallback_url, wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(2)
        except Exception:
            pass
