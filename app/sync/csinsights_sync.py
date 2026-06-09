"""
CS Insights ingestion worker.

Pulls customer-success metrics (health score, sentiment, renewal data)
via the same headless browser bridge used by Insights. Falls back to the
last-known-good snapshot, then demo data, when the portal is unreachable.
"""
from __future__ import annotations

import logging
from typing import Any

from app.connectors.demo_data import generate_cs_summary
from app.sync.base import BaseWorker

logger = logging.getLogger(__name__)


class CSInsightsWorker(BaseWorker):
    source = "csinsights"
    expected_schema = "dict with health_score, sentiment, renewal_date fields"

    def is_available(self) -> bool:
        return self.ctx.csinsights is not None

    async def _sync_account_impl(
        self, account_id: str, account_name: str,
    ) -> dict[str, Any]:
        return await self.ctx.csinsights.get_account_cs_summary(
            account_id, account_name=account_name,
        )

    def fallback_payload(
        self, account_id: str, account_name: str,
    ) -> dict[str, Any] | None:
        prev = self.ctx.db.get_source_snapshot(account_id, self.source)
        if prev:
            prev["_stale_fallback"] = True
            return prev
        demo = generate_cs_summary(account_id)
        demo["_demo"] = True
        return demo
