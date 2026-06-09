"""
CS Insights connector — pulls customer-success account data from
csinsights.internal.example (the "Customer Success 360 Pro" platform).

Data sources (in priority order):
  1. SPA scraping — health/engagement/NPS rendered client-side by the SPA
     (extracted via Playwright page.evaluate when session is live)
  2. CS Insights REST API — account metadata, license adoption data
  3. Salesforce renewal Opportunities — renewal risk score, dates, value
  4. Composite estimates — fallback when live data is unavailable
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.connectors.browser_bridge import PlaywrightBridge

logger = logging.getLogger(__name__)

_RISK_LABEL_TO_SENTIMENT = {
    "On Track": "positive",
    "Internal Processes": "neutral",
    "Field Selling New": "neutral",
    "Product Originally Bundled": "neutral",
    "No Communication": "negative",
    "Pricing / Perceived Value": "negative",
    "Potential Slip": "negative",
    "Economic Health / Budget": "negative",
    "Poor Adoption": "negative",
    "Competition": "negative",
    "Closed Lost": "negative",
    "Partial Churn / Downgrade": "negative",
    "Business Change": "neutral",
}


class CSInsightsConnector:
    """Reads data from the CS Insights (Customer Success 360 Pro) REST API."""

    def __init__(self, bridge: PlaywrightBridge) -> None:
        self._bridge = bridge
        logger.info("CS Insights connector initialized (browser bridge)")

    async def _get(self, path: str, params: Optional[dict] = None) -> Any:
        return await self._bridge.cs_get(path, params)

    async def _post(self, path: str, payload: dict, headers: Optional[dict] = None) -> Any:
        return await self._bridge.cs_post(path, payload, headers)

    # ── User info ────────────────────────────────────────────────────────

    async def get_current_user(self) -> dict[str, Any]:
        return await self._get("/user/me")

    # ── Dashboard data ───────────────────────────────────────────────────

    async def get_eol_version_thresholds(self) -> dict[str, str]:
        """Lowest non-EOL versions for AOS, PC, ROBO."""
        return await self._get("/dashboard/cluster/lowestNonEolVersion")

    async def get_object_urls(self) -> dict[str, Any]:
        """External links (Jira, SFDC) and system alerts."""
        return await self._get("/dashboard/objecturl")

    # ── Product catalogue ────────────────────────────────────────────────

    async def get_product_list(
        self, activated: bool = False, count: bool = False
    ) -> list[dict[str, Any]]:
        return await self._get(
            "/accounts/product/list",
            params={
                "metric": "",
                "isActivated": str(activated).lower(),
                "isCount": str(count).lower(),
            },
        )

    # ── Account search / listing ─────────────────────────────────────────

    async def search_accounts(
        self,
        account_name: str = "",
        page: int = 0,
        page_size: int = 20,
        sort_by: str = "accountName",
        theater: str = "",
        column_names: str = "",
    ) -> dict[str, Any]:
        return await self._post(
            "/documenter/accounts/list",
            payload={
                "pageNo": page,
                "pageSize": page_size,
                "sortBy": sort_by,
                "direction": "",
                "username": "",
                "columnNames": column_names,
                "theater": theater,
                "accountNames": account_name,
                "specialReportId": None,
                "partnerAccountId": "",
                "filterCriteria": {},
            },
            headers={"reportid": "3", "reportname": "accounts"},
        )

    async def get_account_by_name(self, account_name: str) -> Optional[dict[str, Any]]:
        """Look up a single account by name."""
        data = await self.search_accounts(account_name=account_name, page_size=5)
        content = data.get("content", [])
        for acct in content:
            if acct.get("accountName", "").lower() == account_name.lower():
                return acct
        return content[0] if content else None

    async def get_account_by_id(self, account_id: str) -> Optional[dict[str, Any]]:
        """Look up a single account by Salesforce ID (brute search)."""
        data = await self.search_accounts(page_size=50)
        for acct in data.get("content", []):
            if acct.get("accountId") == account_id:
                return acct
        return None

    # ── License / Adoption data ───────────────────────────────────────

    async def search_accounts_licenses(
        self, account_name: str, page_size: int = 5
    ) -> dict[str, Any]:
        """Query the Licenses report view which returns license columns per account."""
        return await self._post(
            "/documenter/accounts/list",
            payload={
                "pageNo": 0,
                "pageSize": page_size,
                "sortBy": "accountName",
                "direction": "",
                "username": "",
                "columnNames": "",
                "theater": "",
                "accountNames": account_name,
                "specialReportId": None,
                "partnerAccountId": "",
                "filterCriteria": {},
            },
            headers={"reportid": "4", "reportname": "licenses"},
        )

    async def get_account_license_data(self, account_name: str) -> dict[str, Any]:
        """
        Fetch per-account license data: capacity, purchased, and activated
        across all Nutanix products. Returns a structured adoption summary.
        """
        capacity_cols = await self.get_product_list(activated=False, count=True)
        quantity_cols = await self.get_product_list(activated=False, count=False)
        activated_cols = await self.get_product_list(activated=True, count=False)

        lic_data = {}
        try:
            lic_data = await self.search_accounts_licenses(account_name)
        except Exception as exc:
            logger.warning("Licenses report query failed: %s", exc)

        content = lic_data.get("content", [])
        acct = None
        for a in content:
            if a.get("accountName", "").lower() == account_name.lower():
                acct = a
                break
        if not acct and content:
            acct = content[0]
        if not acct:
            return {"products": [], "overall_adoption_pct": 0, "total_capacity": 0, "total_activated": 0}

        products: list[dict[str, Any]] = []
        total_cap = 0
        total_act = 0

        for cap_col in capacity_cols:
            label = cap_col["productLabel"]
            cap_key = cap_col["productKey"]
            base = cap_key.replace("CountCapacity", "")
            qty_key = base + "Quantity"
            act_key = base + "QuantityActivated"

            capacity = acct.get(cap_key) or 0
            purchased = acct.get(qty_key) or 0
            activated = acct.get(act_key) or 0

            if capacity == 0 and purchased == 0 and activated == 0:
                continue

            adoption_pct = round((activated / capacity * 100), 1) if capacity > 0 else 0.0
            total_cap += capacity
            total_act += activated

            products.append({
                "product": label,
                "capacity": capacity,
                "purchased": purchased,
                "activated": activated,
                "adoption_pct": adoption_pct,
            })

        overall = round((total_act / total_cap * 100), 1) if total_cap > 0 else 0.0
        products.sort(key=lambda p: p["adoption_pct"])

        return {
            "products": products,
            "total_capacity": total_cap,
            "total_activated": total_act,
            "overall_adoption_pct": overall,
        }

    # ── Aggregated CS summary (for risk scoring) ─────────────────────────

    async def get_account_cs_summary(
        self,
        account_id: str,
        account_name: str = "",
        *,
        sf_renewal: Optional[dict] = None,
    ) -> dict[str, Any]:
        """
        Build a customer-success summary from multiple real data sources.

        Priority chain for each metric:
          1. SPA-scraped value (if CS Insights session is live)
          2. Salesforce renewal Opportunity data (sf_renewal)
          3. CS Insights license adoption API
          4. Composite estimate from account metadata (fallback)

        Each metric includes a ``_source`` sibling field ("live", "salesforce",
        "computed", or "estimated") so the frontend can label data quality.
        """
        # All API calls share the same auth session; if any raises
        # PermissionError the session is dead — skip remaining calls.
        auth_ok = self._bridge.csinsights_authenticated

        acct: Optional[dict] = None
        if account_name and auth_ok:
            try:
                acct = await self.get_account_by_name(account_name)
            except PermissionError:
                auth_ok = False
            except Exception as exc:
                logger.warning("CS account lookup failed: %s", exc)

        # --- SPA scraping (best-effort, only if already authed) ---
        spa_metrics: Optional[dict] = None
        if auth_ok:
            try:
                spa_metrics = await self._bridge.cs_scrape_account_metrics(account_id)
            except Exception as exc:
                logger.debug("SPA scrape unavailable: %s", exc)

        # --- License adoption data (reliable) ---
        adoption_data: dict = {}
        if account_name and auth_ok:
            try:
                adoption_data = await self.get_account_license_data(account_name)
            except PermissionError:
                auth_ok = False
            except Exception as exc:
                logger.warning("License adoption fetch failed: %s", exc)

        # --- Dashboard metadata ---
        eol_versions: dict = {}
        if auth_ok:
            try:
                eol_versions = await self.get_eol_version_thresholds()
            except Exception:
                pass

        system_alerts: list = []
        if auth_ok:
            try:
                obj_urls = await self.get_object_urls()
                system_alerts = obj_urls.get("systemAlerts", [])
            except Exception:
                pass

        if sf_renewal is None:
            sf_renewal = {}

        # --- Build unified summary ---
        days_since_sale = self._days_since_sale(
            acct.get("mostRecentSalesDate") if acct else None
        )
        has_owner = bool(acct.get("accountOwner")) if acct else False
        has_se = bool(acct.get("systemsEngineer")) if acct else False

        # Health score
        health_score, health_source = self._resolve_health(
            spa_metrics, adoption_data, sf_renewal,
            days_since_sale, has_owner, has_se,
        )

        # Engagement score (license adoption is the strongest signal)
        adoption_pct = adoption_data.get("overall_adoption_pct", 0)
        if spa_metrics and spa_metrics.get("engagement_score") is not None:
            engagement = int(spa_metrics["engagement_score"])
            engagement_source = "live"
        elif adoption_pct > 0:
            engagement = int(adoption_pct)
            engagement_source = "computed"
        else:
            engagement = self._fallback_engagement(days_since_sale, has_owner)
            engagement_source = "estimated"

        # Renewal data (from Salesforce Opportunity)
        days_to_renewal = sf_renewal.get("days_to_renewal")
        renewal_date = sf_renewal.get("renewal_date")
        renewal_risk_score = sf_renewal.get("renewal_risk_score")
        renewal_risk_label = sf_renewal.get("renewal_risk_label", "unknown")
        contract_value = sf_renewal.get("contract_value", 0)
        total_renewal_value = sf_renewal.get("total_renewal_value", 0)

        if renewal_risk_score is not None:
            if renewal_risk_score >= 70:
                renewal_risk = "high"
            elif renewal_risk_score >= 40:
                renewal_risk = "medium"
            else:
                renewal_risk = "low"
            renewal_source = "salesforce"
        else:
            renewal_risk = self._fallback_renewal_risk(days_since_sale, health_score)
            renewal_source = "estimated"

        # NPS / Sentiment
        nps_score, sentiment_label, sentiment_source = self._resolve_sentiment(
            spa_metrics, sf_renewal, renewal_risk_label,
        )

        # Risk signals
        signals = self._derive_risk_signals(
            acct or {}, days_since_sale, adoption_data,
            sf_renewal, renewal_risk_label,
        )

        return {
            "account_id": acct.get("accountId", account_id) if acct else account_id,
            "account_name": acct.get("accountName", account_name) if acct else account_name,
            "source": "csinsights.internal.example",
            "health_score": health_score,
            "health_source": health_source,
            "health_trend": "stable",
            "adoption_score": adoption_pct or 50,
            "adoption_source": "computed" if adoption_pct > 0 else "estimated",
            "license_utilization_pct": adoption_pct or 50,
            "adoption_products": adoption_data.get("products", []),
            "total_capacity": adoption_data.get("total_capacity", 0),
            "total_activated": adoption_data.get("total_activated", 0),
            "engagement_score": engagement,
            "engagement_source": engagement_source,
            "nps_score": nps_score,
            "sentiment_label": sentiment_label,
            "sentiment_source": sentiment_source,
            "renewal_date": renewal_date,
            "contract_value": contract_value,
            "total_renewal_value": total_renewal_value,
            "days_to_renewal": days_to_renewal,
            "renewal_risk": renewal_risk,
            "renewal_risk_score": renewal_risk_score,
            "renewal_risk_label": renewal_risk_label,
            "renewal_source": renewal_source,
            "renewal_count": sf_renewal.get("renewal_count", 0),
            "renewals": sf_renewal.get("renewals", []),
            "cs_risk_signals": signals,
            "account_metadata": {
                "region": acct.get("region", "") if acct else "",
                "sub_region": acct.get("subRegion", "") if acct else "",
                "theater": acct.get("theater", "") if acct else "",
                "vertical": acct.get("vertical", "") if acct else "",
                "account_owner": acct.get("accountOwner", "") if acct else "",
                "systems_engineer": acct.get("systemsEngineer", "") if acct else "",
                "most_recent_sale": acct.get("mostRecentSalesDate", "") if acct else "",
                "ai_opt_out": acct.get("aiOptOut", "") if acct else "",
            },
            "eol_version_thresholds": eol_versions,
            "system_alerts": system_alerts,
            "raw": acct or {},
        }

    def _empty_summary(self, account_id: str) -> dict[str, Any]:
        return {
            "account_id": account_id,
            "account_name": "",
            "source": "csinsights.internal.example",
            "health_score": 50,
            "health_source": "estimated",
            "health_trend": "unknown",
            "adoption_score": 0,
            "adoption_source": "estimated",
            "license_utilization_pct": 0,
            "adoption_products": [],
            "total_capacity": 0,
            "total_activated": 0,
            "engagement_score": 50,
            "engagement_source": "estimated",
            "nps_score": None,
            "sentiment_label": "unknown",
            "sentiment_source": "estimated",
            "renewal_date": None,
            "contract_value": 0,
            "total_renewal_value": 0,
            "days_to_renewal": None,
            "renewal_risk": "unknown",
            "renewal_risk_score": None,
            "renewal_risk_label": "unknown",
            "renewal_source": "estimated",
            "renewal_count": 0,
            "renewals": [],
            "cs_risk_signals": [],
            "account_metadata": {},
            "eol_version_thresholds": {},
            "system_alerts": [],
            "raw": {},
        }

    # ── Metric resolution (multi-source priority chain) ───────────────

    @staticmethod
    def _resolve_health(
        spa: Optional[dict],
        adoption: dict,
        sf_renewal: dict,
        days_since_sale: Optional[int],
        has_owner: bool,
        has_se: bool,
    ) -> tuple[int, str]:
        """Return (score, source_label)."""
        if spa and spa.get("health_score") is not None:
            return int(spa["health_score"]), "live"

        # Composite from adoption + renewal risk + metadata
        signals = 0
        total_weight = 0

        adoption_pct = adoption.get("overall_adoption_pct", 0)
        if adoption_pct > 0:
            signals += adoption_pct * 0.4
            total_weight += 0.4

        risk_score = sf_renewal.get("renewal_risk_score")
        if risk_score is not None:
            signals += (100 - risk_score) * 0.3
            total_weight += 0.3

        meta_score = 50
        if has_owner:
            meta_score += 10
        if has_se:
            meta_score += 10
        if days_since_sale is not None:
            if days_since_sale < 180:
                meta_score += 15
            elif days_since_sale > 730:
                meta_score -= 20
        signals += meta_score * 0.3
        total_weight += 0.3

        if total_weight > 0:
            score = int(signals / total_weight)
        else:
            score = 50

        source = "computed" if (adoption_pct > 0 or risk_score is not None) else "estimated"
        return max(10, min(100, score)), source

    @staticmethod
    def _resolve_sentiment(
        spa: Optional[dict],
        sf_renewal: dict,
        risk_label: str,
    ) -> tuple[Optional[int], str, str]:
        """Return (nps_score_or_None, sentiment_label, source)."""
        if spa and spa.get("nps_score") is not None:
            nps = int(spa["nps_score"])
            label = "positive" if nps >= 30 else ("neutral" if nps >= 0 else "negative")
            return nps, label, "live"

        if risk_label and risk_label != "unknown":
            label = _RISK_LABEL_TO_SENTIMENT.get(risk_label, "neutral")
            return None, label, "salesforce"

        return None, "unknown", "estimated"

    @staticmethod
    def _fallback_engagement(days_since_sale: Optional[int], has_owner: bool) -> int:
        score = 50
        if days_since_sale is not None:
            if days_since_sale < 180:
                score += 15
            elif days_since_sale > 730:
                score -= 20
        if has_owner:
            score += 10
        return max(10, min(100, score))

    @staticmethod
    def _fallback_renewal_risk(days_since_sale: Optional[int], health: int) -> str:
        if health < 40:
            return "high"
        if days_since_sale and days_since_sale > 730:
            return "medium"
        return "low"

    @staticmethod
    def _days_since_sale(date_str: Optional[str]) -> Optional[int]:
        if not date_str:
            return None
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).days
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _derive_risk_signals(
        acct: dict,
        days_since_sale: Optional[int],
        adoption: dict,
        sf_renewal: dict,
        risk_label: str,
    ) -> list[str]:
        signals: list[str] = []

        # Account coverage gaps
        if not acct.get("accountOwner"):
            signals.append("No account owner assigned")
        if not acct.get("systemsEngineer"):
            signals.append("No systems engineer assigned")

        # Purchase recency
        if days_since_sale is not None and days_since_sale > 730:
            signals.append(f"No purchase in {days_since_sale} days — potential churn risk")
        elif days_since_sale is not None and days_since_sale > 365:
            signals.append(f"Last purchase {days_since_sale} days ago")

        # License adoption gaps
        products = adoption.get("products", [])
        for p in products:
            if p.get("capacity", 0) > 0 and p.get("adoption_pct", 0) == 0:
                signals.append(
                    f"{p['product']} purchased but 0% activated"
                )
        overall_adoption = adoption.get("overall_adoption_pct", 0)
        if 0 < overall_adoption < 30:
            signals.append(
                f"Overall license adoption only {overall_adoption:.0f}%"
            )

        # Renewal risk signals from Salesforce
        if risk_label in ("Potential Slip", "No Communication"):
            signals.append(f"Renewal flagged: {risk_label}")
        elif risk_label in ("Poor Adoption", "Competition",
                            "Partial Churn / Downgrade"):
            signals.append(f"Renewal at risk: {risk_label}")
        elif risk_label in ("Economic Health / Budget",
                            "Pricing / Perceived Value"):
            signals.append(f"Renewal concern: {risk_label}")

        risk_score = sf_renewal.get("renewal_risk_score")
        if risk_score is not None and risk_score >= 70:
            signals.append(
                f"High renewal risk score: {risk_score:.0f}%"
            )

        days_to = sf_renewal.get("days_to_renewal")
        if days_to is not None and days_to < 90:
            signals.append(
                f"Renewal due in {days_to} days"
            )

        return signals
