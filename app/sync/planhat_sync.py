"""
Planhat ingestion worker.

Pulls customer-success snapshots (health, lifecycle, NPS, renewals,
activities, tasks/conversations) via the headless browser bridge.
Falls back to the last-known-good snapshot when the Planhat session
is unreachable; there is no demo-data fallback for Planhat in v1.
"""
from __future__ import annotations

import logging
from typing import Any

from app.sync.base import BaseWorker

logger = logging.getLogger(__name__)


class PlanhatWorker(BaseWorker):
    source = "planhat"
    expected_schema = (
        "dict with health_score, lifecycle_phase, renewal_date, nps_score, "
        "open_tasks, open_conversations fields"
    )

    def is_available(self) -> bool:
        return self.ctx.planhat is not None

    async def _sync_account_impl(
        self, account_id: str, account_name: str,
    ) -> dict[str, Any]:
        return await self.ctx.planhat.get_account_planhat_summary(
            account_id, account_name=account_name,
        )

    def fallback_payload(
        self, account_id: str, account_name: str,
    ) -> dict[str, Any] | None:
        prev = self.ctx.db.get_source_snapshot(account_id, self.source)
        if prev:
            prev["_stale_fallback"] = True
            return prev
        return None
