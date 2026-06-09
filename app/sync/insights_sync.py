"""
Nutanix Insights ingestion worker.

Two execution modes share one worker:

* **Bridge mode** — the headless Chromium daemon is up and the portal
  session is authenticated. We hit ``portal.example.com`` for everything
  the SPA exposes, falling back to SF clusters only when the live call
  fails.
* **SF+cache mode** — the bridge is absent (no daemon, no Okta cookies,
  or both). We build the same payload shape from Salesforce ``Cluster__c``
  records cross-referenced with the global advisory / EOL cache populated
  by :class:`app.sync.portal_globals_sync.PortalGlobalsWorker`. This mode
  is what makes "Resync now" actually do something when no one has run
  ``scripts/browser_setup.py``.

The worker is "available" whenever either mode is viable, so the sync
scheduler keeps treating Insights as a first-class source.
"""
from __future__ import annotations

import logging
from typing import Any

from app.connectors.demo_data import generate_insights_health
from app.connectors.nutanix_insights import NutanixInsightsConnector
from app.sync.base import BaseWorker
from app.sync.portal_globals_sync import load_cached_globals

logger = logging.getLogger(__name__)


class InsightsWorker(BaseWorker):
    source = "insights"
    expected_schema = "dict with cluster/alert/advisory fields"

    def is_available(self) -> bool:
        # Bridge connector is the richest path; SF connector is the
        # always-on fallback. Either is enough to produce useful data.
        return self.ctx.insights is not None or self.ctx.sf is not None

    def _sf_clusters(self, account_id: str) -> list[dict[str, Any]]:
        """Pull SF clusters from the most recent SF snapshot for fallback."""
        sf_snap = self.ctx.db.get_source_snapshot(account_id, "salesforce")
        if not sf_snap:
            return []
        return sf_snap.get("clusters") or []

    async def _sync_account_impl(
        self, account_id: str, account_name: str,
    ) -> dict[str, Any]:
        sf_clusters = self._sf_clusters(account_id)

        if self.ctx.insights is not None:
            return await self.ctx.insights.get_account_health_summary(
                account_name, account_id=account_id, sf_clusters=sf_clusters,
            )

        # SF+cache mode — no exception, just compute synthetically. Raising
        # here would push us into the AI-heal retry loop for a perfectly
        # normal "no portal session" condition.
        if not sf_clusters:
            raise ValueError(
                "no Salesforce cluster snapshot for account; "
                "need a SF sync to run first"
            )
        cached = load_cached_globals(self.ctx.db)
        return NutanixInsightsConnector.build_health_summary_from_sf(
            sf_clusters, account_name, cached_globals=cached,
        )

    def fallback_payload(
        self, account_id: str, account_name: str,
    ) -> dict[str, Any] | None:
        # Strategy:
        # 1. If we have SF clusters, build a health summary from them
        #    enriched with cached global advisories/EOL — better than
        #    demo, marked accordingly.
        # 2. Else if we have a previous Insights snapshot, return that.
        # 3. Else generate demo data so the UI still renders.
        sf_clusters = self._sf_clusters(account_id)
        if sf_clusters:
            try:
                cached = load_cached_globals(self.ctx.db)
                payload = NutanixInsightsConnector.build_health_summary_from_sf(
                    sf_clusters, account_name, cached_globals=cached,
                )
                payload["_built_from"] = "salesforce_clusters+cache"
                return payload
            except Exception as exc:
                logger.warning(
                    "Insights fallback (SF+cache) failed for %s: %s",
                    account_id, exc,
                )

        prev = self.ctx.db.get_source_snapshot(account_id, self.source)
        if prev:
            prev["_stale_fallback"] = True
            return prev

        demo = generate_insights_health(account_name)
        demo["_demo"] = True
        return demo
