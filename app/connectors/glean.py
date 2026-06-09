"""
Internal engagement connector — surfaces internal activity signals
for customer accounts by querying Salesforce Task, Event, and
CaseComment objects.

This replaces the previous Glean enterprise-search connector.  All
data comes from the existing Salesforce connection — no additional
credentials, API tokens, or browser sessions are required.

Signals extracted per account:
  - Total internal touchpoints (tasks + events + internal comments)
  - Recent activity count (last 30 days)
  - Escalation / P1-P2 case count
  - Internal case comment count (knowledge proxy)
  - Source breakdown (Tasks, Meetings, Internal Comments)
  - Activity trend (increasing / stable / declining)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.connectors.salesforce import SalesforceConnector

logger = logging.getLogger(__name__)


class InternalEngagementConnector:
    """
    Thin wrapper around SalesforceConnector.get_internal_engagement().
    Keeps the same aggregator/risk-scorer contract that the former
    GleanConnector provided.
    """

    def __init__(self, sf: Optional[SalesforceConnector] = None) -> None:
        self._sf = sf
        logger.info(
            "Internal engagement connector initialized (mode: %s)",
            "live" if sf else "demo",
        )

    @property
    def live(self) -> bool:
        return self._sf is not None

    async def get_account_engagement_summary(
        self,
        account_id: str,
        account_name: str = "",
    ) -> dict[str, Any]:
        """
        Fetch internal engagement data from Salesforce.
        Returns the same dict shape consumed by the risk scorer.
        """
        if not self._sf:
            return {}

        data = await asyncio.to_thread(
            self._sf.get_internal_engagement, account_id
        )
        data["account_name"] = account_name
        return data
