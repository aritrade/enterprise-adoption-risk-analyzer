"""
Glean (internal-engagement) ingestion worker.

The signal is Salesforce-derived (Tasks / Events / internal CaseComments)
but is treated as a separate source so the scheduler can refresh it
independently of the main SF account snapshot.
"""
from __future__ import annotations

import logging
from typing import Any

from app.connectors.demo_data import generate_engagement_summary
from app.sync.base import BaseWorker

logger = logging.getLogger(__name__)


class GleanWorker(BaseWorker):
    source = "glean"
    expected_schema = "dict with total_mentions, recent_mentions_30d, top_results fields"

    def is_available(self) -> bool:
        return self.ctx.glean is not None

    async def _sync_account_impl(
        self, account_id: str, account_name: str,
    ) -> dict[str, Any]:
        return await self.ctx.glean.get_account_engagement_summary(
            account_id, account_name=account_name,
        )

    def fallback_payload(
        self, account_id: str, account_name: str,
    ) -> dict[str, Any] | None:
        prev = self.ctx.db.get_source_snapshot(account_id, self.source)
        if prev:
            prev["_stale_fallback"] = True
            return prev
        demo = generate_engagement_summary(account_id, account_name)
        demo["_demo"] = True
        return demo
