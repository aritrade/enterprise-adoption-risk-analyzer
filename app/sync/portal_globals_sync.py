"""
Portal-wide reference data refresher.

Most "Insights" risk signals are not customer-scoped — they're global lists
(security advisories, field advisories, EOL versions, product alerts) that
get cross-referenced against each customer's cluster fingerprint to score
exposure. We refresh these on a 2-hour cadence into ``portal_globals_cache``
so per-account scoring keeps working even when the headless browser bridge
is offline or its Okta cookies have expired.

Sources, in order of preference for each blob:

1. ``portal_public`` (cookie-less HTTPS) — only the security-advisory
   endpoint is reachable this way today; everything else 403s.
2. ``PlaywrightBridge`` (authenticated Okta session) — used for
   field advisories, EOL versions, alerts. Skipped if the bridge or its
   portal session isn't up.
3. The previously-cached blob, served with ``provenance='stale'`` so the
   sync UI can show that the data is past its expiry window.

The worker is deliberately tolerant: a partial run is treated as ``ok`` if
at least one blob got refreshed (typically the public security feed), so
the operator sees a green status even when the bridge is down.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from app.connectors import portal_public
from app.store.db import Database
from app.sync.base import WorkerContext, WorkerResult

logger = logging.getLogger(__name__)


# Cache keys are stable — readers (the InsightsWorker, sync UI) look these
# up by name. Bumping a key forces a refresh on next run.
KEY_SECURITY_ADVISORIES = "portal:security_advisories"
KEY_FIELD_ADVISORIES = "portal:field_advisories"
KEY_EOL_VERSIONS = "portal:eol_versions"
KEY_ALERTS = "portal:alerts"

DEFAULT_TTL_SECONDS = 6 * 60 * 60  # serve as "fresh" for 6h, "stale" after


@dataclass
class _BlobResult:
    key: str
    ok: bool
    provenance: str
    detail: str = ""


class PortalGlobalsWorker:
    """Refresh global portal data into ``portal_globals_cache``.

    Has its own ``sync_now`` entry point because it doesn't fit the
    per-account ``BaseWorker`` shape (no account_id involved). The
    Scheduler invokes it like the other sources but routes through this
    method instead of ``sync_account``.
    """

    source = "portal_globals"
    expected_schema = "dict of cached blobs keyed by cache_key"

    def __init__(self, ctx: WorkerContext) -> None:
        self.ctx = ctx
        self.db: Database = ctx.db

    def is_available(self) -> bool:
        # We can always at least try the public security-advisory feed.
        return True

    async def sync_now(self) -> WorkerResult:
        """Fetch every global blob, persisting whatever succeeds."""
        started = time.time()
        result = WorkerResult(source=self.source, account_id="_global")

        results: list[_BlobResult] = await asyncio.gather(
            self._refresh_security_advisories(),
            self._refresh_field_advisories(),
            self._refresh_eol_versions(),
            self._refresh_alerts(),
        )

        ok_count = sum(1 for r in results if r.ok)
        notes = ", ".join(
            f"{r.key.split(':', 1)[1]}={r.provenance}"
            + (f"({r.detail})" if r.detail else "")
            for r in results
        )

        # Use the same status vocabulary as the rest of the scheduler so the
        # observability filters that count "ok|partial" treat us correctly.
        if ok_count == 0:
            result.status = "failed"
            result.error = notes
        elif ok_count < len(results):
            result.status = "partial"
            result.error = notes
        else:
            result.status = "ok"

        result.payload = {
            "blobs": [
                {
                    "key": r.key, "ok": r.ok,
                    "provenance": r.provenance, "detail": r.detail,
                }
                for r in results
            ],
        }
        result.duration_ms = int((time.time() - started) * 1000)
        logger.info("portal_globals refresh — %s (%s)", result.status, notes)
        return result

    # ── Per-blob refreshers ──────────────────────────────────────────

    async def _refresh_security_advisories(self) -> _BlobResult:
        """Always reachable — public endpoint, no auth."""
        try:
            advisories = await portal_public.fetch_security_advisories()
            self.db.upsert_global_cache(
                cache_key=KEY_SECURITY_ADVISORIES,
                payload={"advisories": advisories},
                provenance="unauth",
                ttl_seconds=DEFAULT_TTL_SECONDS,
            )
            return _BlobResult(
                KEY_SECURITY_ADVISORIES, True, "unauth",
                detail=f"{len(advisories)} items",
            )
        except Exception as exc:
            return self._fallback_to_cache(
                KEY_SECURITY_ADVISORIES, exc,
            )

    async def _refresh_field_advisories(self) -> _BlobResult:
        async def _fetch() -> Any:
            data = await self.ctx.bridge.portal_get("/pages/fieldAdvisories")
            if isinstance(data, dict):
                return data.get("content", {}).get("body", []) or []
            return []
        return await self._refresh_via_bridge(
            key=KEY_FIELD_ADVISORIES, payload_label="advisories",
            fetcher=_fetch,
        )

    async def _refresh_eol_versions(self) -> _BlobResult:
        async def _fetch() -> Any:
            data = await self.ctx.bridge.portal_get("/pages/eolVersions")
            return data if isinstance(data, dict) else {}
        return await self._refresh_via_bridge(
            key=KEY_EOL_VERSIONS, payload_label="eol",
            fetcher=_fetch,
        )

    async def _refresh_alerts(self) -> _BlobResult:
        async def _fetch() -> Any:
            data = await self.ctx.bridge.portal_get(
                "/alerts",
                params={
                    "isKBSubscribedWithOtherAlerts": "true",
                    "limit": 200,
                    "sort[]": ["alertDate DESC", "priority ASC"],
                },
            )
            return data if isinstance(data, list) else []
        return await self._refresh_via_bridge(
            key=KEY_ALERTS, payload_label="alerts",
            fetcher=_fetch,
        )

    # ── Helpers ──────────────────────────────────────────────────────

    async def _refresh_via_bridge(
        self,
        key: str,
        payload_label: str,
        fetcher: Callable[[], Awaitable[Any]],
    ) -> _BlobResult:
        bridge = self.ctx.bridge
        if bridge is None or not getattr(bridge, "portal_authenticated", False):
            return self._fallback_to_cache(
                key, RuntimeError("portal bridge unauthenticated"),
            )
        try:
            data = await fetcher()
        except Exception as exc:
            return self._fallback_to_cache(key, exc)

        # Bridge fetchers return either list[dict] (advisories/alerts) or
        # dict (EOL). Normalise to a wrapper dict so cache consumers get
        # a stable shape regardless of source.
        if isinstance(data, list):
            wrapped = {payload_label: data}
            count_desc = f"{len(data)} items"
        elif isinstance(data, dict):
            wrapped = {payload_label: data}
            count_desc = f"{len(data)} keys"
        else:
            wrapped = {payload_label: data}
            count_desc = type(data).__name__

        self.db.upsert_global_cache(
            cache_key=key, payload=wrapped,
            provenance="bridge", ttl_seconds=DEFAULT_TTL_SECONDS,
        )
        return _BlobResult(key, True, "bridge", detail=count_desc)

    def _fallback_to_cache(self, key: str, exc: BaseException) -> _BlobResult:
        """Re-flag the existing blob as stale; return a non-ok result."""
        existing = self.db.get_global_cache(key)
        detail = f"{type(exc).__name__}: {exc}"[:150]
        if existing is None:
            return _BlobResult(key, False, "missing", detail=detail)
        # Re-stamp with provenance='stale' so the UI can see the blob
        # didn't refresh on this cycle. We deliberately do NOT bump
        # fetched_at — operators want to see how old the data really is.
        # Since upsert_global_cache always sets fetched_at=now, we
        # short-circuit here and write directly.
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE portal_globals_cache
                SET provenance = 'stale'
                WHERE cache_key = ?
                """,
                (key,),
            )
        return _BlobResult(key, False, "stale", detail=detail)


# ── Convenience accessor ────────────────────────────────────────────

def load_cached_globals(db: Database) -> dict[str, dict[str, Any]]:
    """Return every cached global blob keyed by short name (e.g. ``security``).

    Used by the Insights worker / fallback summary builder. Missing keys
    are simply absent — readers must tolerate that.
    """
    out: dict[str, dict[str, Any]] = {}
    for cache_key, short in (
        (KEY_SECURITY_ADVISORIES, "security_advisories"),
        (KEY_FIELD_ADVISORIES, "field_advisories"),
        (KEY_EOL_VERSIONS, "eol_versions"),
        (KEY_ALERTS, "alerts"),
    ):
        rec = db.get_global_cache(cache_key)
        if rec is None:
            continue
        out[short] = rec
    return out
