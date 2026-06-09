"""
Aggregator — the FastAPI application's read-side facade over the
ingestion DB.

What changed (background-sync architecture):

* Risk analyses are no longer fetched per request. The
  :class:`app.sync.scheduler.Scheduler` runs every 2 hours, hydrates
  ``data/app.db`` via per-source workers, recomputes risk profiles, and
  pre-builds pie-chart aggregates. The aggregator now reads from those
  tables.
* On a stale or missing snapshot, the aggregator triggers the scheduler
  to refresh that single account on demand and re-reads the result.
* Low-frequency, user-triggered queries (license entitlements, contacts,
  financials, raw cluster summaries) still call the live Salesforce
  connector — they aren't part of the recurring sync.

Public surface area (used by ``app/main.py``) is unchanged.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from cachetools import TTLCache

from app.config import settings
from app.connectors import demo_data
from app.connectors.csinsights import CSInsightsConnector
from app.connectors.planhat import PlanhatConnector
from app.connectors.glean import InternalEngagementConnector
from app.connectors.nutanix_insights import NutanixInsightsConnector
from app.connectors.outlook import OutlookConnector
from app.connectors.employee_verify import verify_employee_count
from app.connectors.salesforce import SalesforceConnector
from app.engine.email_generator import generate_email_draft
from app.engine.risk_scorer import AccountRiskProfile, RiskScorer
from app.store.custom_risks import CustomRiskStore
from app.store.db import get_db
from app.sync.base import WorkerContext
from app.sync.scheduler import (
    AggregateRecomputer, ProfileRecomputer, Scheduler,
)

logger = logging.getLogger(__name__)


# Per-request TTL cache for genuinely on-demand queries (license,
# financials, contacts) — these aren't part of the 2-hour sync.
_cache: TTLCache[str, dict[str, Any]] = TTLCache(
    maxsize=500, ttl=settings.refresh_interval_minutes * 60
)


class Aggregator:
    def __init__(self) -> None:
        self._sf_live = settings.sf_live and bool(settings.sf_username)
        self._insights_live = settings.insights_live
        self._csinsights_live = settings.csinsights_live
        self._planhat_live = settings.planhat_live
        self._engagement_live = self._sf_live and settings.engagement_live
        self._insights_expired = False
        self._csinsights_expired = False
        self._planhat_expired = False
        self._engagement_expired = False

        self.sf: Optional[SalesforceConnector] = (
            SalesforceConnector() if self._sf_live else None
        )

        self._bridge = None
        if self._insights_live or self._csinsights_live or self._planhat_live:
            try:
                from app.connectors.browser_bridge import PlaywrightBridge
                self._bridge = PlaywrightBridge()
            except ImportError:
                logger.error(
                    "Playwright not installed — portal connectors disabled. "
                    "Run: pip install playwright && playwright install chromium"
                )
                self._insights_live = False
                self._csinsights_live = False
                self._planhat_live = False

        self.insights: Optional[NutanixInsightsConnector] = None
        if self._insights_live and self._bridge:
            self.insights = NutanixInsightsConnector(self._bridge)

        self.csinsights: Optional[CSInsightsConnector] = None
        if self._csinsights_live and self._bridge:
            self.csinsights = CSInsightsConnector(self._bridge)

        self.planhat: Optional[PlanhatConnector] = None
        if self._planhat_live and self._bridge:
            self.planhat = PlanhatConnector(self._bridge)

        self.engagement: Optional[InternalEngagementConnector] = None
        if self._engagement_live and self.sf:
            self.engagement = InternalEngagementConnector(self.sf)

        self.scorer = RiskScorer()
        self.custom_risk_store = CustomRiskStore()
        self.outlook = OutlookConnector()

        self._demo = not (
            self._sf_live
            or self._insights_live
            or self._csinsights_live
            or self._planhat_live
        )
        self.db = get_db()

        # Scheduler owns the per-source workers and the cron loop.
        self._worker_ctx = WorkerContext(
            sf=self.sf, insights=self.insights,
            csinsights=self.csinsights, planhat=self.planhat,
            glean=self.engagement,
            bridge=self._bridge, db=self.db,
        )
        self.scheduler = Scheduler(self._worker_ctx)
        self._profile_recompute = ProfileRecomputer(
            self.scorer, self.custom_risk_store,
        )
        self._aggregate_recompute = AggregateRecomputer()

        logger.info(
            "Connector modes — SF: %s | Insights: %s | CS Insights: %s | "
            "Planhat: %s | Glean: %s",
            "LIVE" if self._sf_live else "DEMO",
            "LIVE (browser)" if self._insights_live else "DEMO",
            "LIVE (browser)" if self._csinsights_live else "DEMO",
            "LIVE (browser)" if self._planhat_live else "DEMO",
            "LIVE (SF)" if self._engagement_live else "DEMO",
        )

    @property
    def connector_status(self) -> dict[str, str]:
        def _state(live: bool, expired: bool) -> str:
            if not live:
                return "demo"
            return "expired" if expired else "live"
        insights_state = _state(self._insights_live, self._insights_expired)
        if insights_state != "live" and self._sf_live:
            # The InsightsWorker still produces useful data via SF clusters
            # plus the public portal cache (security advisories) — even with
            # no Okta session. Reflect that in the status badge so the UI
            # doesn't claim "demo" for what is actually live data.
            insights_state = "live (SF+cache)"
        glean_state = _state(self._engagement_live, self._engagement_expired)
        return {
            "salesforce": "live" if self._sf_live else "demo",
            "insights": insights_state,
            "csinsights": _state(self._csinsights_live, self._csinsights_expired),
            "planhat": _state(self._planhat_live, self._planhat_expired),
            "glean": glean_state,
            "engagement": glean_state,
        }

    # ── Account index (read from DB, refreshed by the SF worker) ─────

    async def load_account_index(self) -> None:
        """
        Kept for backwards compatibility. The real work happens in the
        :class:`SalesforceWorker.sync_index` job; here we just trigger
        an immediate run so the DB is populated on first launch.
        """
        if self._sf_live:
            try:
                await self.scheduler.sync_index_now()
                return
            except Exception as exc:
                logger.error("Account index sync failed: %s", exc)

        # Demo bootstrap: write demo accounts into the DB so subsequent
        # paginated reads work without a live SF connection. Region / CXM /
        # industry are assigned deterministically so the region filter and
        # CXM-portfolio views populate exactly like the live build.
        existing = self.db.get_account_count()
        if existing == 0:
            _regions = ["APJ", "EMEA", "AMER"]
            _cxms = [
                ("Aria Menon", "aria.menon@example.com"),
                ("Dev Kapoor", "dev.kapoor@example.com"),
                ("Sam Fernandez", "sam.fernandez@example.com"),
            ]
            _industries = [
                "Financial Services", "Technology", "Manufacturing",
                "Telecom", "Retail", "Healthcare", "Media",
            ]
            demo_rows = []
            for idx, a in enumerate(demo_data.DEMO_ACCOUNTS):
                cxm_name, cxm_email = _cxms[idx % len(_cxms)]
                demo_rows.append({
                    "id": a["id"], "name": a["name"], "type": "Customer",
                    "industry": _industries[idx % len(_industries)],
                    "owner": _cxms[(idx + 1) % len(_cxms)][0],
                    "cxm": cxm_name, "cxm_email": cxm_email,
                    "cxm_program": "Premier" if idx % 2 == 0 else "Standard",
                    "name_lower": a["name"].lower(),
                    "region": _regions[idx % len(_regions)],
                })
            self.db.replace_account_index(demo_rows)
            logger.info("Account index loaded (demo): %d accounts", len(demo_rows))

    def get_account_index_stats(self) -> dict[str, Any]:
        return {
            "total_accounts": self.db.get_account_count(),
            "index_ready": True,
            "freshness": self.db.source_freshness(),
        }

    def search_accounts_fast(
        self,
        query: str,
        limit: int = 50,
        region: Optional[str] = None,
    ) -> list[dict[str, str]]:
        return self.db.search_accounts(query, limit=limit, region=region)

    def get_all_accounts_page(
        self,
        offset: int = 0,
        limit: int = 100,
        region: Optional[str] = None,
    ) -> dict[str, Any]:
        total = self.db.get_account_count(region=region)
        accounts = self.db.list_accounts(offset=offset, limit=limit, region=region)
        return {
            "total": total, "offset": offset, "limit": limit,
            "accounts": accounts, "region": region or "all",
        }

    def get_cxm_roster(
        self, region: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        return self.db.cxm_roster(region=region)

    def get_region_counts(self) -> dict[str, int]:
        return self.db.region_counts()

    def get_accounts_by_cxm(
        self,
        cxm_name: str,
        offset: int = 0,
        limit: int = 100,
        region: Optional[str] = None,
    ) -> dict[str, Any]:
        accounts, total = self.db.list_accounts_by_cxm(
            cxm_name, offset=offset, limit=limit, region=region,
        )
        return {
            "total": total, "offset": offset, "limit": limit,
            "accounts": accounts, "filter_cxm": cxm_name,
        }

    # ── Account analysis (DB-backed with on-demand fallback) ─────────

    def _profile_to_response(self, profile: dict[str, Any], age_minutes: int) -> dict[str, Any]:
        out = dict(profile)
        out["demo_mode"] = self._demo
        out["connector_modes"] = self.connector_status
        out["data_age_minutes"] = age_minutes
        out["data_is_stale"] = age_minutes > settings.sync_stale_after_hours * 60
        return out

    async def _on_demand_sync(self, account_id: str, account_name: str) -> None:
        """Trigger the scheduler to refresh one account; logged but non-fatal."""
        try:
            await self.scheduler.sync_account_now(account_id, account_name)
        except Exception as exc:
            logger.warning(
                "On-demand sync for %s failed (%s); falling back to demo profile",
                account_id, exc,
            )

    def _demo_profile(self, account_id: str, account_name: str) -> dict[str, Any]:
        sf = demo_data.generate_sf_account_summary(account_id, account_name)
        ins = demo_data.generate_insights_health(account_name)
        cs = demo_data.generate_cs_summary(account_id)
        glean = demo_data.generate_engagement_summary(account_id, account_name)
        custom = self.custom_risk_store.list_for_account(account_id)
        profile: AccountRiskProfile = self.scorer.score_account(
            account_id=account_id, account_name=account_name,
            sf_data=sf, insights_data=ins, cs_data=cs,
            custom_risks=custom, glean_data=glean,
        )
        result = profile.to_dict()
        result["custom_risks"] = custom
        result["data_age_minutes"] = 0
        result["data_is_stale"] = False
        result["demo_mode"] = True
        result["connector_modes"] = self.connector_status
        return result

    async def analyze_account(
        self, account_id: str, account_name: str, *, bypass_cache: bool = False,
    ) -> dict[str, Any]:
        """
        Read the account's risk profile from the DB; refresh on demand
        if missing or stale.
        """
        if bypass_cache:
            await self._on_demand_sync(account_id, account_name)

        row = self.db.get_risk_profile(account_id)
        age = row.age_minutes() if row else -1
        stale = (
            row is None
            or age < 0
            or age > settings.sync_stale_after_hours * 60
        )

        if stale and (
            self._sf_live
            or self._insights_live
            or self._csinsights_live
            or self._planhat_live
        ):
            await self._on_demand_sync(account_id, account_name)
            row = self.db.get_risk_profile(account_id)
            age = row.age_minutes() if row else -1

        if row is None:
            # No live connectors and no cached snapshot — emit a demo
            # profile and persist it so the next read is fast.
            result = self._demo_profile(account_id, account_name)
            self.db.upsert_risk_profile(account_id, account_name, result)
            self._aggregate_recompute.recompute_account(account_id, result)
            return result

        result = self._profile_to_response(row.profile, age)
        result["custom_risks"] = self.custom_risk_store.list_for_account(account_id)
        return result

    async def analyze_multiple(
        self, accounts: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """
        Bulk read profiles from the DB. Accounts missing a profile get
        an on-demand sync (concurrency-bounded so a 100-account top-risk
        request doesn't fan out to 100 simultaneous SF queries).
        """
        sem = asyncio.Semaphore(max(1, settings.sync_account_concurrency))

        async def _one(acct: dict[str, str]) -> dict[str, Any]:
            async with sem:
                return await self.analyze_account(
                    acct["account_id"], acct["account_name"],
                )

        return list(await asyncio.gather(*[_one(a) for a in accounts]))

    # ── Top risk accounts ────────────────────────────────────────────

    async def get_top_risk_accounts(
        self, limit: int = 20, region: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Read the top-N profiles from the DB, ordered by overall_score."""
        rows = self.db.top_risk_profiles(limit=limit, region=region)
        if rows:
            return [self._profile_to_response(r.profile, r.age_minutes()) for r in rows]

        # Cold start (DB empty) — fall back to demo set. The demo bootstrap
        # has no region awareness; when a region filter is applied we return
        # an empty set rather than mis-attributing demo accounts.
        if region and region != "all":
            return []
        accounts = [
            {"account_id": a["id"], "account_name": a["name"]}
            for a in demo_data.DEMO_ACCOUNTS
        ]
        results = await self.analyze_multiple(accounts)
        results.sort(key=lambda r: r.get("overall_score", 0), reverse=True)
        return results[:limit]

    # ── Search ───────────────────────────────────────────────────────

    async def search_and_analyze(self, search_term: str) -> list[dict[str, Any]]:
        matches = self.db.search_accounts(search_term, limit=10)
        if not matches:
            return []
        accounts = [
            {"account_id": m["id"], "account_name": m["name"]} for m in matches
        ]
        return await self.analyze_multiple(accounts)

    # ── My Accounts (TAM portfolio) ──────────────────────────────────

    async def get_my_accounts(
        self, username: str, limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not self._csinsights_live:
            # Demo mode: match the requested user against the seeded
            # owner / CXM fields so the "My Accounts" portfolio view works
            # without a live CS Insights connection.
            def _norm(s: str) -> str:
                return (s or "").lower().replace(".", " ").strip()
            needle = _norm(username)
            first = needle.split(" ")[0] if needle else ""
            page = self.db.list_accounts(offset=0, limit=1000)
            mine = [
                {"account_id": a["id"], "account_name": a["name"]}
                for a in page
                if needle and (
                    needle in _norm(a.get("owner", ""))
                    or needle in _norm(a.get("cxm", ""))
                    or (first and first in _norm(a.get("owner", "")).split(" "))
                    or (first and first in _norm(a.get("cxm", "")).split(" "))
                )
            ]
            if not mine:
                return []
            results = await self.analyze_multiple(mine[:limit])
            results.sort(key=lambda r: r.get("overall_score", 0), reverse=True)
            return results
        try:
            data = await self.csinsights.search_accounts(page_size=200)
            content = data.get("content", [])
            mine = [
                a for a in content
                if username.lower() in (a.get("accountOwnerUsername") or "").lower()
                or username.lower() in (a.get("systemsEngineerUsername") or "").lower()
            ]
            accounts = [
                {"account_id": a["accountId"], "account_name": a["accountName"]}
                for a in mine
            ]
            if not accounts:
                return []
            results = await self.analyze_multiple(accounts[:limit])
            results.sort(key=lambda r: r.get("overall_score", 0), reverse=True)
            return results
        except Exception as exc:
            logger.error("My Accounts fetch failed: %s", exc)
            return []

    # ── On-demand SF queries (license / contacts / financials) ───────

    def _resolve_account_name(self, account_id: str) -> str:
        acct = self.db.get_account(account_id)
        return acct["name"] if acct else ""

    async def get_account_financials(self, account_id: str) -> dict[str, Any]:
        cache_key = f"financials:{account_id}"
        if cache_key in _cache:
            return _cache[cache_key]
        if not self._sf_live:
            return {"account_id": account_id}
        try:
            result = await asyncio.to_thread(
                self.sf.get_account_financials, account_id,
            )
            account_name = self._resolve_account_name(account_id)
            if account_name:
                ev = await asyncio.to_thread(
                    verify_employee_count, account_name,
                    sf_standard=result.get("number_of_employees"),
                    sf_dб=result.get("dnb_employees"),
                    sf_imputed=result.get("imputed_employees"),
                )
                result["employee_verification"] = ev
            _cache[cache_key] = result
            return result
        except Exception as exc:
            logger.error("SF financials fetch failed: %s", exc)
            return {"account_id": account_id}

    async def get_license_analysis(
        self, account_id: str, account_name: str,
    ) -> dict[str, Any]:
        cache_key = f"license:{account_id}"
        if cache_key in _cache:
            return _cache[cache_key]

        entitlements, sf_licenses = await asyncio.gather(
            self._fetch_license_entitlements(account_id),
            self._fetch_sf_licenses(account_id),
        )

        products = entitlements.get("products", [])
        sf_families = sf_licenses.get("families", [])
        sw_products = [p for p in products if not p.get("product", "").startswith("_")]
        total_cap = sum(p.get("total", 0) for p in sw_products)
        total_used = sum(p.get("used", 0) for p in sw_products)
        overall_pct = round(total_used / total_cap * 100) if total_cap > 0 else 0

        from app.connectors.salesforce import _NCP_SOFTWARE_FAMILIES
        _FAMILY_DISPLAY = {
            "NCI": "NCI", "NCI-Edge": "NCI-Edge", "NCI-VDI": "NCI-VDI",
            "NCM": "NCM", "NCM-Edge": "NCM-Edge", "NKP": "NKP",
            "NUS": "NUS", "NDB": "NDB", "NAI": "NAI",
            "NC2": "NC2", "NDK": "NDK", "NDL": "NDL",
        }
        _FAMILY_ORDER = list(_FAMILY_DISPLAY.keys())

        family_buckets: dict[str, dict[str, Any]] = {}
        for p in products:
            family = p.get("product", "")
            if family.startswith("_") or family not in _NCP_SOFTWARE_FAMILIES:
                continue
            fb = family_buckets.setdefault(family, {
                "family": _FAMILY_DISPLAY.get(family, family),
                "used": 0, "total": 0,
                "metric": p.get("metric", ""), "tiers": [],
            })
            fb["used"] += p.get("used", 0)
            fb["total"] += p.get("total", 0)
            tier = p.get("tier", "")
            fb["tiers"].append({
                "product": family, "tier": tier,
                "used": p.get("used", 0), "total": p.get("total", 0),
                "adoption_pct": p.get("adoption_pct", 0),
                "metric": p.get("metric", ""),
            })
            if p.get("metric"):
                fb["metric"] = p["metric"]

        for fb in family_buckets.values():
            fb["adoption_pct"] = (
                round(fb["used"] / fb["total"] * 100) if fb["total"] > 0 else 0
            )
            fb["available"] = max(0, fb["total"] - fb["used"])
        ordered = {f: i for i, f in enumerate(_FAMILY_ORDER)}
        product_families = sorted(
            family_buckets.values(), key=lambda f: ordered.get(f["family"], 999),
        )

        recommendations = self._generate_adoption_recommendations(
            products, sf_families, overall_pct, account_name,
        )

        result = {
            "account_id": account_id,
            "account_name": account_name,
            "overall_adoption_pct": overall_pct,
            "total_capacity": total_cap,
            "total_activated": total_used,
            "products": products,
            "product_families": product_families,
            "sf_asset_families": sf_families,
            "sf_total_assets": sf_licenses.get("total_assets", 0),
            "total_licenses": entitlements.get("total_licenses", 0),
            "recommendations": recommendations,
        }
        _cache[cache_key] = result
        return result

    async def _fetch_license_entitlements(self, account_id: str) -> dict[str, Any]:
        if not self._sf_live:
            return {"account_id": account_id, "total_licenses": 0, "products": []}
        try:
            return await asyncio.to_thread(
                self.sf.get_license_entitlements, account_id,
            )
        except Exception as exc:
            logger.error("SF license entitlement fetch failed: %s", exc)
            return {"account_id": account_id, "total_licenses": 0, "products": []}

    async def _fetch_sf_licenses(self, account_id: str) -> dict[str, Any]:
        if not self._sf_live:
            return {"account_id": account_id, "total_assets": 0, "families": []}
        try:
            return await asyncio.to_thread(
                self.sf.get_license_summary, account_id,
            )
        except Exception as exc:
            logger.error("SF license fetch failed: %s", exc)
            return {"account_id": account_id, "total_assets": 0, "families": []}

    @staticmethod
    def _generate_adoption_recommendations(
        products: list[dict],
        sf_families: list[dict],
        overall_pct: float,
        account_name: str,
    ) -> list[dict[str, Any]]:
        recs: list[dict[str, Any]] = []

        if overall_pct > 0 and overall_pct < 30:
            recs.append({
                "priority": "critical", "area": "Overall Adoption",
                "message": f"Overall license adoption is only {overall_pct}% — urgent adoption engagement needed.",
                "action": "Schedule an adoption workshop with the customer to identify blockers and create a deployment roadmap.",
            })
        elif overall_pct > 0 and overall_pct < 60:
            recs.append({
                "priority": "high", "area": "Overall Adoption",
                "message": f"License adoption at {overall_pct}% — significant room for growth.",
                "action": "Review underutilized products and create a targeted enablement plan.",
            })

        PRODUCT_ACTIONS = {
            "NCI": "Nutanix Cloud Infrastructure — core HCI platform. Check for unlicensed clusters or capacity expansion.",
            "NCM": "Nutanix Cloud Manager — intelligent operations, cost governance. Demonstrate anomaly detection and capacity planning.",
            "NKP": "Nutanix Kubernetes Platform — engage platform engineering teams for container orchestration on Nutanix.",
            "NUS": "Nutanix Unified Storage — Files, Objects, Block storage. Identify NAS/object workloads for migration.",
            "NDB": "Nutanix Database Service — propose a PoC for their top database workloads (PostgreSQL, Oracle, SQL Server).",
            "NC2": "Nutanix Cloud Clusters — hybrid cloud. Identify workloads suitable for burst/DR in public cloud.",
            "NDK": "Nutanix Data Services for Kubernetes — engage DevOps teams for stateful container storage.",
            "AOS": "Core platform — check if customer has unlicensed clusters or capacity expansion planned.",
            "Calm": "Automation & orchestration — offer a Calm workshop to demonstrate self-service IaaS/app lifecycle management.",
            "Era": "Database management — propose an Era PoC for their top database workloads.",
            "Files": "File storage — identify NAS/file share workloads for migration to Nutanix Files.",
            "Flow": "Microsegmentation — run a Flow discovery to map east-west traffic and propose security policies.",
            "Objects": "Object storage — identify S3-compatible workloads (backup targets, unstructured data, ML/AI pipelines).",
            "Prism Pro": "Advanced monitoring — demonstrate anomaly detection and capacity planning benefits.",
            "Security Central": "Security posture — demonstrate compliance reporting and vulnerability management.",
            "Leap": "Disaster recovery — propose DR planning for business-critical applications.",
            "NKE": "Kubernetes — engage platform engineering teams for container orchestration.",
        }

        for p in products:
            pct = p.get("adoption_pct", 0)
            cap = p.get("total", 0) or p.get("capacity", 0)
            act = p.get("used", 0) or p.get("activated", 0)
            name = p.get("product", "")

            if cap == 0:
                continue

            cap_i, act_i = int(cap), int(act)
            tier = p.get("tier", "")
            label = f"{name} {tier}".strip() if tier else name

            if pct == 0 and cap_i > 0:
                action = PRODUCT_ACTIONS.get(name, f"Engage customer on {label} deployment — they have {cap_i:,} capacity with zero activation.")
                recs.append({
                    "priority": "critical", "area": label,
                    "message": f"{label}: {cap_i:,} capacity purchased, 0 activated — complete shelfware.",
                    "action": action,
                })
            elif pct < 25:
                action = PRODUCT_ACTIONS.get(name, f"Plan adoption drive for {label}.")
                recs.append({
                    "priority": "high", "area": label,
                    "message": f"{label}: only {pct}% adopted ({act_i:,}/{cap_i:,} active).",
                    "action": action,
                })
            elif pct < 50:
                recs.append({
                    "priority": "medium", "area": label,
                    "message": f"{label}: {pct}% adopted ({act_i:,}/{cap_i:,}) — room for improvement.",
                    "action": PRODUCT_ACTIONS.get(name, f"Review deployment plan for {label}."),
                })

        expired_families = [
            f for f in sf_families if f.get("expired_quantity", 0) > 0
        ]
        for fam in expired_families:
            recs.append({
                "priority": "high", "area": fam["family"],
                "message": f"{fam['family']}: {fam['expired_quantity']} expired license(s) in SFDC — renewal needed.",
                "action": "Coordinate with renewals team to process license renewal before customer loses entitlement.",
            })

        if not products and sf_families:
            owned = {f["family"] for f in sf_families if f.get("total_quantity", 0) > 0}
            all_ntnx = {"AOS", "VDI", "Files", "Calm", "Flow", "Objects", "Era",
                        "NCM (Prism Pro)", "Mine", "Leap", "NKE (Kubernetes)",
                        "Security Central"}
            not_owned = all_ntnx - owned - {"AOS", "Hardware"}
            if not_owned:
                top_opps = sorted(not_owned)[:4]
                recs.append({
                    "priority": "medium", "area": "Cross-sell",
                    "message": f"Customer owns {len(owned)} product families but not: {', '.join(top_opps)}.",
                    "action": "Identify workloads that could benefit from " + ", ".join(top_opps[:2]) + " and propose a PoC.",
                })

        recs.sort(key=lambda r: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(r["priority"], 9))
        return recs

    # ── Email outreach ───────────────────────────────────────────────

    async def generate_email_draft(
        self,
        account_id: str,
        account_name: str,
        sender_name: str = "",
        include_license: bool = False,
    ) -> dict[str, Any]:
        risk_data = await self.analyze_account(account_id, account_name)

        contacts: list[dict] = []
        if self._sf_live:
            try:
                contacts = await asyncio.to_thread(
                    self.sf.get_account_contacts, account_id,
                )
            except Exception as exc:
                logger.error("Failed to fetch contacts for %s: %s", account_id, exc)
        if not contacts:
            contacts = demo_data.generate_contacts(account_id, account_name)

        license_data: dict[str, Any] | None = None
        if include_license:
            try:
                license_data = await self.get_license_analysis(account_id, account_name)
            except Exception as exc:
                logger.warning("License data fetch failed for email: %s", exc)

        return generate_email_draft(
            risk_data=risk_data, contacts=contacts,
            sender_name=sender_name, include_license=include_license,
            license_data=license_data,
        )

    async def get_account_contacts(
        self, account_id: str, account_name: str,
    ) -> list[dict[str, Any]]:
        if self._sf_live:
            try:
                return await asyncio.to_thread(
                    self.sf.get_account_contacts, account_id,
                )
            except Exception as exc:
                logger.error("SF contacts fetch failed: %s", exc)
        return demo_data.generate_contacts(account_id, account_name)

    async def send_email_via_outlook(
        self, to: list[str], cc: list[str], subject: str,
        body_html: str, token: Optional[str] = None,
    ) -> dict[str, Any]:
        if not self.outlook.enabled:
            raise RuntimeError(
                "Outlook integration is not configured. "
                "Set MS_GRAPH_CLIENT_ID and MS_GRAPH_TENANT_ID in .env"
            )
        return await self.outlook.send_email(
            to=to, cc=cc, subject=subject,
            body_html=body_html, token=token,
        )

    async def create_outlook_draft(
        self, to: list[str], cc: list[str], subject: str,
        body_html: str, token: Optional[str] = None,
    ) -> dict[str, Any]:
        if not self.outlook.enabled:
            raise RuntimeError("Outlook integration is not configured.")
        return await self.outlook.create_draft(
            to=to, cc=cc, subject=subject,
            body_html=body_html, token=token,
        )

    # ── Pre-warm: portal auth + scheduler bootstrap ─────────────────

    async def prewarm(self) -> None:
        """
        Authenticate the portal browser bridge (if configured) in
        parallel with the scheduler's first sync. This replaces the old
        per-request fetch path entirely — by the time the FastAPI app
        starts serving, the DB has the account index and the scheduler
        is ticking on its 2-hour cron.
        """

        async def _auth_portals() -> None:
            if not self._bridge:
                return
            try:
                await self._bridge.start()
            except Exception as exc:
                logger.error(
                    "Browser bridge failed: %s  "
                    "Start the daemon: python scripts/browser_daemon.py --bg  "
                    "Then setup:       python scripts/browser_setup.py",
                    exc,
                )
                self._insights_live = False
                self._csinsights_live = False
                self._planhat_live = False
                self._worker_ctx.insights = None
                self._worker_ctx.csinsights = None
                self._worker_ctx.planhat = None
                self.insights = None
                self.csinsights = None
                self.planhat = None
                return

            if self._insights_live:
                try:
                    ok = await self._bridge.ensure_portal_auth()
                    if not ok:
                        logger.warning("Portal auth failed at startup — Insights falling back to demo")
                        self._insights_live = False
                        self._insights_expired = True
                        self._worker_ctx.insights = None
                        self.insights = None
                except Exception as exc:
                    logger.warning("Portal auth error: %s — Insights falling back to demo", exc)
                    self._insights_live = False
                    self._insights_expired = True
                    self._worker_ctx.insights = None
                    self.insights = None

            if self._csinsights_live:
                try:
                    ok = await self._bridge.ensure_cs_auth()
                    if not ok:
                        logger.warning("CS Insights auth failed at startup — falling back to demo")
                        self._csinsights_live = False
                        self._csinsights_expired = True
                        self._worker_ctx.csinsights = None
                        self.csinsights = None
                except Exception as exc:
                    logger.warning("CS Insights auth error: %s — falling back to demo", exc)
                    self._csinsights_live = False
                    self._csinsights_expired = True
                    self._worker_ctx.csinsights = None
                    self.csinsights = None

            if self._planhat_live:
                try:
                    ok = await self._bridge.ensure_planhat_auth()
                    if not ok:
                        logger.warning(
                            "Planhat auth failed at startup — disabling Planhat connector. "
                            "Run `python scripts/planhat_login.py` to (re-)authenticate."
                        )
                        self._planhat_live = False
                        self._planhat_expired = True
                        self._worker_ctx.planhat = None
                        self.planhat = None
                except Exception as exc:
                    logger.warning(
                        "Planhat auth error: %s — disabling Planhat connector", exc,
                    )
                    self._planhat_live = False
                    self._planhat_expired = True
                    self._worker_ctx.planhat = None
                    self.planhat = None

        async def _bootstrap_index() -> None:
            try:
                await self.load_account_index()
            except Exception as exc:
                logger.warning("Account index bootstrap failed (non-fatal): %s", exc)

        await asyncio.gather(_bootstrap_index(), _auth_portals())

        if self._demo:
            try:
                seeded = demo_data.seed_demo_custom_risks()
                if seeded:
                    logger.info("Seeded %d demo CXM risk flags", seeded)
            except Exception as exc:
                logger.warning("Demo CXM risk seeding failed (non-fatal): %s", exc)
            try:
                await self._seed_demo_portfolio()
            except Exception as exc:
                logger.warning("Demo portfolio seeding failed (non-fatal): %s", exc)

        # Always refresh the portal-globals cache once at startup, even when
        # sync_enabled=false (the dev mode that disables the recurring cron).
        # The InsightsWorker reads this cache for advisory matching, so an
        # empty cache means missing risk signals on first use.
        async def _initial_globals_refresh() -> None:
            try:
                await self.scheduler.sync_source_now("portal_globals")
            except Exception:
                logger.exception("Initial portal_globals refresh failed")

        asyncio.create_task(_initial_globals_refresh())

        try:
            await self.scheduler.start(run_now=False)
        except Exception:
            logger.exception("Scheduler failed to start")

    async def _seed_demo_portfolio(self) -> None:
        """Pre-compute risk profiles + aggregates for every demo account so the
        Top Risks tab, portfolio risk donut, and breakdowns populate on first
        load — mirroring what the background scheduler does in the live build."""
        for a in demo_data.DEMO_ACCOUNTS:
            try:
                result = self._demo_profile(a["id"], a["name"])
                self.db.upsert_risk_profile(a["id"], a["name"], result)
                self._aggregate_recompute.recompute_account(a["id"], result)
            except Exception:
                logger.debug("demo seed failed for %s", a["id"], exc_info=True)
        for level in ("high", "medium", "all"):
            try:
                self._aggregate_recompute.recompute_portfolio(level)
            except Exception:
                logger.debug("demo portfolio aggregate failed for %s", level, exc_info=True)
        logger.info("Demo portfolio seeded: %d accounts analysed", len(demo_data.DEMO_ACCOUNTS))

    async def shutdown(self) -> None:
        try:
            await self.scheduler.shutdown()
        except Exception:
            logger.debug("Scheduler shutdown error", exc_info=True)
        if self._bridge:
            try:
                await self._bridge.stop()
            except Exception:
                pass

    def clear_cache(self) -> int:
        count = len(_cache)
        _cache.clear()
        return count

    # ── Risk-breakdown reads (pie chart) ────────────────────────────

    def get_account_risk_breakdown(self, account_id: str) -> dict[str, Any]:
        slices = self.db.get_aggregates("account", account_id)
        if not slices:
            row = self.db.get_risk_profile(account_id)
            if row is None:
                return {"account_id": account_id, "total": 0, "slices": []}
            self._aggregate_recompute.recompute_account(account_id, row.profile)
            slices = self.db.get_aggregates("account", account_id)
        total = round(sum(s["weighted_total"] for s in slices), 2)
        for s in slices:
            s["pct"] = round(s["weighted_total"] / total * 100, 1) if total > 0 else 0
        return {"account_id": account_id, "total": total, "slices": slices}

    def get_portfolio_risk_breakdown(self, level: str = "high") -> dict[str, Any]:
        slices = self.db.get_aggregates("portfolio", level)
        if not slices:
            self._aggregate_recompute.recompute_portfolio(level)
            slices = self.db.get_aggregates("portfolio", level)
        total = round(sum(s["weighted_total"] for s in slices), 2)
        for s in slices:
            s["pct"] = round(s["weighted_total"] / total * 100, 1) if total > 0 else 0
        return {
            "level": level,
            "threshold": (
                settings.risk_threshold_high if level == "high"
                else settings.risk_threshold_medium if level == "medium"
                else 0
            ),
            "total": total,
            "slices": slices,
        }
