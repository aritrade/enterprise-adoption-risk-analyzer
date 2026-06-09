"""
Salesforce ingestion worker.

Pulls everything the risk scorer needs from a single SF org per account
in one consolidated snapshot:

* Case summary (open / P1 / P2 / escalated)
* Cluster__c records
* Renewal Opportunity outlook
* Account financials
* Account owner

The full customer-account index is refreshed by :meth:`sync_index`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.sync.base import BaseWorker

logger = logging.getLogger(__name__)


class SalesforceWorker(BaseWorker):
    source = "salesforce"
    expected_schema = (
        "{summary: dict, clusters: list, renewal: dict, financials: dict, owner: dict}"
    )

    def is_available(self) -> bool:
        return self.ctx.sf is not None

    async def sync_index(self) -> int:
        """
        Replace the entire ``accounts`` table with a fresh pull from
        SFDC. Returns the row count written.
        """
        if not self.is_available():
            return 0
        accounts = await asyncio.to_thread(self.ctx.sf.get_all_customer_accounts)
        return self.ctx.db.replace_account_index(accounts)

    async def _sync_account_impl(
        self, account_id: str, account_name: str,
    ) -> dict[str, Any]:
        sf = self.ctx.sf
        # Run the four reads concurrently — each is its own SOQL call.
        summary, clusters, renewal, financials, owner = await asyncio.gather(
            asyncio.to_thread(sf.get_account_summary, account_id),
            asyncio.to_thread(sf.get_account_clusters, account_id),
            asyncio.to_thread(sf.get_renewal_outlook, account_id),
            asyncio.to_thread(sf.get_account_financials, account_id),
            asyncio.to_thread(sf.get_account_owner_email, account_id),
            return_exceptions=True,
        )
        # We tolerate per-call partial failures (e.g. renewal field on a
        # custom-object that's not enabled in this org). The summary call
        # is the only required one — everything else degrades to {}.
        if isinstance(summary, BaseException):
            raise summary

        def _ok(value: Any, default: Any) -> Any:
            return value if not isinstance(value, BaseException) else default

        return {
            "summary": summary,
            "clusters": _ok(clusters, []),
            "renewal": _ok(renewal, {}),
            "financials": _ok(financials, {}),
            "owner": _ok(owner, {}),
        }

    def fallback_payload(
        self, account_id: str, account_name: str,
    ) -> dict[str, Any] | None:
        # Use the previous snapshot if we have one; otherwise leave the
        # row absent so the aggregator switches to demo data.
        prev = self.ctx.db.get_source_snapshot(account_id, self.source)
        if prev:
            prev["_stale_fallback"] = True
            return prev
        return None
