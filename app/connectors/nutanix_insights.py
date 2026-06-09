"""
Nutanix Insights Portal connector — pulls account data, cluster warnings,
alerts, contract/EOL information, per-node infrastructure data, and
security/field advisories from portal.example.com/api/v1/.

Authentication is handled by the PlaywrightBridge, which uses a headless
Chromium browser with a persistent profile. The browser manages Okta SSO
sessions and portal cookies automatically — no manual cookie export needed.
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from app.connectors.browser_bridge import PlaywrightBridge

logger = logging.getLogger(__name__)

_PULSE_STALE_DAYS = 30


class NutanixInsightsConnector:
    """Reads data from the Nutanix Support & Insights portal REST API."""

    def __init__(self, bridge: PlaywrightBridge) -> None:
        self._bridge = bridge
        logger.info("Insights connector initialized (browser bridge)")

    async def _get(self, path: str, params: dict | None = None) -> Any:
        return await self._bridge.portal_get(path, params)

    async def _post(self, path: str, payload: dict | None = None) -> Any:
        return await self._bridge.portal_post(path, payload)

    # ── Customer accounts ────────────────────────────────────────────────

    async def get_customer_accounts(self) -> list[dict[str, Any]]:
        """All customer accounts visible to the authenticated user."""
        data = await self._get(
            "/customeraccounts",
            params={"type": "Customer,Nutanix Internal Use", "sort": "name ASC"},
        )
        return data if isinstance(data, list) else []

    async def get_account_details(self, account_id: str) -> dict[str, Any]:
        """Detailed account info (industry, type, owner, billing address)."""
        data = await self._get("/accounts", params={"id": account_id})
        if isinstance(data, list) and data:
            return data[0]
        return {}

    # ── Cases (from portal perspective) ──────────────────────────────────

    async def get_open_cases(self, account_id: str) -> list[dict[str, Any]]:
        """Open support cases for an account via the portal API."""
        data = await self._get(
            "/cases",
            params={
                "limit": 200,
                "caseRecordType": "Support",
                "skip": 0,
                "accountId": account_id,
                "isClosed": "false",
            },
        )
        return data if isinstance(data, list) else []

    # ── Cluster warnings & alerts ────────────────────────────────────────

    async def get_cluster_warnings(self) -> list[dict[str, Any]]:
        """Cluster health warnings from the portal."""
        data = await self._get("/clusterwarning")
        warnings = data.get("data", []) if isinstance(data, dict) else data
        return warnings if isinstance(warnings, list) else []

    async def get_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        """Product/release alerts and notifications."""
        data = await self._get(
            "/alerts",
            params={
                "isKBSubscribedWithOtherAlerts": "true",
                "limit": limit,
                "sort[]": ["alertDate DESC", "priority ASC"],
            },
        )
        return data if isinstance(data, list) else []

    # ── Account health metadata ──────────────────────────────────────────

    async def get_account_portal_values(self, account_id: str) -> dict[str, Any]:
        """Account health data: contract status, AOS versions, Pulse status."""
        data = await self._post(
            "/accountportalvalues",
            payload={"id": account_id},
        )
        result: dict[str, Any] = {}
        if isinstance(data, list):
            for item in data:
                if item.get("id") == account_id:
                    result = item
                    break
            if not result and data:
                result = data[0]
        elif isinstance(data, dict):
            result = data

        if result:
            logger.debug(
                "Portal values for %s: %s",
                account_id,
                json.dumps(result, default=str)[:500],
            )
        return result

    # ── EOL information ──────────────────────────────────────────────────

    async def get_eol_versions(self) -> dict[str, list[dict]]:
        """End-of-life software versions across all products."""
        data = await self._get("/pages/eolVersions")
        return data if isinstance(data, dict) else {}

    # ── Per-node asset data ──────────────────────────────────────────────
    # NOTE: /assets is NOT account-scoped — it always returns the
    # authenticated user's own linked assets regardless of accountId.
    # Kept for potential self-account diagnostics.

    async def get_account_assets(self, account_id: str) -> list[dict[str, Any]]:
        """Fetch per-node/block asset data (authenticated user's assets only)."""
        data = await self._get(
            "/assets",
            params={"accountId": account_id, "limit": 500},
        )
        return data if isinstance(data, list) else []

    # ── Account-scoped asset summary via page scraping ──────────────────

    async def get_account_asset_summary(self, account_id: str) -> dict[str, Any]:
        """
        Get real cluster count, node count, and pulse breakdown for an
        account by scraping the portal's Assets page.

        The portal REST API (/accountportalvalues) only provides limited
        aggregates (AOS version map, a single pulse boolean).  The actual
        cluster/node/pulse counts come from Pulse telemetry data that the
        portal SPA renders on the Assets page.  We navigate there and
        extract the numbers.
        """
        scraped = await self._bridge.portal_scrape_account_assets(account_id)
        if not scraped:
            return {}

        summary: dict[str, Any] = {}
        if scraped.get("clusters") is not None:
            summary["real_cluster_count"] = scraped["clusters"]
        if scraped.get("nodes") is not None:
            summary["real_node_count"] = scraped["nodes"]
        if scraped.get("pulse_active") is not None:
            summary["pulse_active_clusters"] = scraped["pulse_active"]
        if scraped.get("pulse_disabled") is not None:
            summary["pulse_disabled_clusters"] = scraped["pulse_disabled"]
        if scraped.get("_cluster_list"):
            summary["clusters_raw"] = scraped["_cluster_list"]
        return summary

    # ── Advisories ───────────────────────────────────────────────────────

    async def get_security_advisories(self) -> list[dict[str, Any]]:
        """Fetch all Nutanix security advisories."""
        data = await self._get("/pages/securityAdvisories")
        if isinstance(data, dict):
            return data.get("content", {}).get("body", [])
        return []

    async def get_field_advisories(self) -> list[dict[str, Any]]:
        """Fetch all Nutanix field advisories."""
        data = await self._get("/pages/fieldAdvisories")
        if isinstance(data, dict):
            return data.get("content", {}).get("body", [])
        return []

    # ── Asset aggregation ────────────────────────────────────────────────

    @staticmethod
    def _aggregate_assets(raw_assets: list[dict]) -> dict[str, Any]:
        """Group raw asset records by clusterId into per-cluster summaries."""
        by_cluster: dict[str, list[dict]] = defaultdict(list)
        for asset in raw_assets:
            cid = str(asset.get("clusterId") or asset.get("serialNumber") or "unknown")
            by_cluster[cid].append(asset)

        clusters: list[dict[str, Any]] = []
        versions_agg: dict[str, dict[str, int]] = {
            "AOS": defaultdict(int),
            "Hypervisor": defaultdict(int),
            "PC": defaultdict(int),
            "NCC": defaultdict(int),
        }
        hardware_agg: dict[str, int] = defaultdict(int)
        now = datetime.now(timezone.utc)

        for cid, nodes in by_cluster.items():
            first = nodes[0]
            node_count = sum(n.get("numNodes", 1) or 1 for n in nodes)

            aos = first.get("nosVersion") or "unknown"
            hv_type = first.get("hypervisorType") or ""
            hv_ver = first.get("hypervisorVersion") or ""
            hypervisor = f"{hv_type} {hv_ver}".strip() or "unknown"
            pc = first.get("pcVersion") or None
            ncc = first.get("nccVersion") or None

            models = sorted({n.get("modelName") or "unknown" for n in nodes})
            platforms = sorted({n.get("platform") or n.get("modelFamily") or "" for n in nodes})
            total_cores = sum(n.get("cores") or 0 for n in nodes)
            memories = [n.get("perNodeMemory") or "" for n in nodes if n.get("perNodeMemory")]
            total_ssd_bytes = sum(n.get("ssd") or 0 for n in nodes)
            total_hdd_bytes = sum(n.get("hdd") or 0 for n in nodes)

            contract_statuses = {n.get("contractStatus") or "unknown" for n in nodes}
            if "ACTIVE" in {s.upper() for s in contract_statuses}:
                contract_status = "Active"
            elif "EXPIRED" in {s.upper() for s in contract_statuses}:
                contract_status = "Expired"
            else:
                contract_status = next(iter(contract_statuses), "unknown")
            contract_end = first.get("contractEndDate")

            pulse_dates = [
                n.get("lastPulseDate") for n in nodes if n.get("lastPulseDate")
            ]
            last_pulse = max(pulse_dates) if pulse_dates else None
            pulse_active = False
            if last_pulse:
                try:
                    lp = datetime.fromisoformat(
                        last_pulse.replace("+0000", "+00:00").replace("Z", "+00:00")
                    )
                    pulse_active = (now - lp).days < _PULSE_STALE_DAYS
                except (ValueError, TypeError):
                    pass

            compression = any(n.get("compressionEnabled") for n in nodes)
            dedup = any(n.get("dedupEnabled") for n in nodes)

            component_versions: dict[str, str | None] = {}
            for key, label in [
                ("foundationVersion", "Foundation"),
                ("lcmVersion", "LCM"),
                ("bucketsVersion", "Objects"),
                ("calmVersion", "Calm"),
                ("eraVersion", "NDB"),
                ("fileAnalyticsVersion", "File Analytics"),
                ("nutanixFilesVersion", "Files"),
                ("karbonVersion", "NKE"),
            ]:
                val = first.get(key)
                if val:
                    component_versions[label] = val

            versions_agg["AOS"][aos] += node_count
            versions_agg["Hypervisor"][hypervisor] += node_count
            if pc:
                versions_agg["PC"][pc] += node_count
            if ncc:
                versions_agg["NCC"][ncc] += node_count
            for m in models:
                hardware_agg[m] += node_count

            clusters.append({
                "cluster_id": cid,
                "node_count": node_count,
                "aos_version": aos,
                "hypervisor": hypervisor,
                "hypervisor_type": hv_type,
                "pc_version": pc,
                "ncc_version": ncc,
                "hardware_models": models,
                "platforms": platforms,
                "contract_status": contract_status,
                "contract_end": contract_end,
                "pulse_active": pulse_active,
                "last_pulse": last_pulse,
                "total_cores": total_cores,
                "memory_per_node": memories[0] if memories else None,
                "total_ssd_tb": round(total_ssd_bytes / 1e12, 1) if total_ssd_bytes else 0,
                "total_hdd_tb": round(total_hdd_bytes / 1e12, 1) if total_hdd_bytes else 0,
                "compression": compression,
                "dedup": dedup,
                "component_versions": component_versions,
                "coverage": first.get("coverage") or "",
            })

        clusters.sort(key=lambda c: c["aos_version"])
        total_nodes = sum(c["node_count"] for c in clusters)

        return {
            "clusters": clusters,
            "total_clusters": len(clusters),
            "total_nodes": total_nodes,
            "versions_summary": {k: dict(v) for k, v in versions_agg.items()},
            "hardware_summary": dict(hardware_agg),
        }

    # ── Advisory matching ────────────────────────────────────────────────

    @staticmethod
    def _match_advisories(
        advisories: list[dict],
        advisory_type: str,
        clusters: list[dict],
    ) -> list[dict[str, Any]]:
        """
        Cross-reference advisories against the customer's running
        AOS versions, hypervisor versions, and hardware platforms.
        Returns enriched advisory dicts with per-cluster affected details
        and match reasons so the UI can name specific assets.
        """
        if not advisories or not clusters:
            return []

        customer_aos = {c["aos_version"] for c in clusters if c.get("aos_version")}
        customer_hv = set()
        for c in clusters:
            hv = c.get("hypervisor", "")
            if hv:
                customer_hv.add(hv.lower())
            hv_type = c.get("hypervisor_type", "")
            if hv_type:
                customer_hv.add(hv_type.lower())
        customer_platforms = set()
        for c in clusters:
            for p in c.get("platforms", []):
                if p:
                    customer_platforms.add(p.lower())
            for m in c.get("hardware_models", []):
                if m:
                    customer_platforms.add(m.lower())

        cluster_lookup = {c["cluster_id"]: c for c in clusters}
        matched: list[dict[str, Any]] = []

        for adv in advisories:
            affected = (adv.get("affectedVersions") or "").lower()
            if not affected:
                continue

            if affected.startswith("third party product"):
                continue

            hit = False
            hit_cluster_ids: set[str] = set()
            match_reasons: list[str] = []

            for aos_ver in customer_aos:
                if aos_ver == "unknown" or aos_ver == "Unknown":
                    continue
                if aos_ver.lower() in affected:
                    hit = True
                    match_reasons.append(f"AOS {aos_ver} in affected range")
                    for c in clusters:
                        if c["aos_version"] == aos_ver:
                            hit_cluster_ids.add(c["cluster_id"])
                parts = aos_ver.split(".")
                if len(parts) >= 2:
                    major_minor = f"{parts[0]}.{parts[1]}"
                    if major_minor in affected and "aos" in affected:
                        hit = True
                        if f"AOS {aos_ver}" not in " ".join(match_reasons):
                            match_reasons.append(f"AOS {major_minor}.x in affected range")
                        for c in clusters:
                            if c["aos_version"].startswith(major_minor):
                                hit_cluster_ids.add(c["cluster_id"])

            for platform in customer_platforms:
                if not platform or platform == "unknown":
                    continue
                if platform in affected:
                    hit = True
                    match_reasons.append(f"Hardware {platform.upper()} matches")
                    for c in clusters:
                        pset = {p.lower() for p in c.get("platforms", []) + c.get("hardware_models", [])}
                        if platform in pset:
                            hit_cluster_ids.add(c["cluster_id"])
                gen_match = re.search(r'[Gg](\d+)', platform)
                if gen_match:
                    gen = gen_match.group(0).upper()
                    if gen in affected.upper():
                        hit = True
                        if f"Hardware {platform.upper()}" not in " ".join(match_reasons):
                            match_reasons.append(f"Platform generation {gen} matches")
                        for c in clusters:
                            pset = {p.lower() for p in c.get("platforms", []) + c.get("hardware_models", [])}
                            if platform in pset:
                                hit_cluster_ids.add(c["cluster_id"])

            if "ahv" in affected:
                for c in clusters:
                    if (c.get("hypervisor_type") or "").lower() == "ahv":
                        hit = True
                        hit_cluster_ids.add(c["cluster_id"])
                if hit and "AHV hypervisor" not in " ".join(match_reasons):
                    match_reasons.append("AHV hypervisor affected")
            if "esxi" in affected:
                for c in clusters:
                    hv = (c.get("hypervisor") or "").lower()
                    if "esxi" in hv:
                        hit = True
                        hit_cluster_ids.add(c["cluster_id"])
                if hit and "ESXi" not in " ".join(match_reasons):
                    match_reasons.append("ESXi hypervisor affected")
            if "hyper-v" in affected or "hyperv" in affected:
                for c in clusters:
                    hv = (c.get("hypervisor") or "").lower()
                    if "hyper" in hv:
                        hit = True
                        hit_cluster_ids.add(c["cluster_id"])
                if hit and "Hyper-V" not in " ".join(match_reasons):
                    match_reasons.append("Hyper-V hypervisor affected")

            if "prism central" in affected or "pc." in affected:
                for c in clusters:
                    if c.get("pc_version"):
                        hit = True
                        hit_cluster_ids.add(c["cluster_id"])
                if hit:
                    match_reasons.append("Prism Central affected")

            if "all versions" in affected or "all nx" in affected:
                hit = True
                hit_cluster_ids = {c["cluster_id"] for c in clusters}
                match_reasons.append("All versions/platforms affected")

            if hit:
                severity = (
                    adv.get("severity")
                    or adv.get("criticality")
                    or ""
                ).strip() or "Info"

                affected_cluster_details = []
                for cid in sorted(hit_cluster_ids):
                    c = cluster_lookup.get(cid, {})
                    affected_cluster_details.append({
                        "cluster_id": cid,
                        "aos_version": c.get("aos_version", ""),
                        "node_count": c.get("node_count", 0),
                        "hardware_models": c.get("hardware_models", []),
                        "hypervisor": c.get("hypervisor", ""),
                    })

                matched.append({
                    "type": advisory_type,
                    "number": adv.get("advisoryNumber", ""),
                    "title": adv.get("title", ""),
                    "severity": severity,
                    "date": adv.get("date", ""),
                    "affected_versions": adv.get("affectedVersions", ""),
                    "summary": adv.get("summary", ""),
                    "href": adv.get("href", ""),
                    "affected_cluster_count": len(hit_cluster_ids),
                    "affected_clusters": affected_cluster_details,
                    "match_reasons": match_reasons,
                })

        matched.sort(key=lambda a: (
            {"critical": 0, "high": 1, "medium": 2}.get(a["severity"].lower(), 3),
            a["date"],
        ))
        return matched

    # ── Build cluster list from Salesforce Cluster__c records ──────────

    _PULSE_VERSION_FIELDS = (
        "NCC_Version_Pulse__c", "PC_Version_Pulse__c",
        "LCM_Version_Pulse__c", "Foundation_Version_Pulse__c",
        "AFS_Version_Pulse__c", "Buckets_version_Pulse__c",
    )

    @staticmethod
    def _clusters_from_sf(
        sf_clusters: list[dict[str, Any]],
        now: datetime,
        pulse_stale: timedelta,
    ) -> list[dict[str, Any]]:
        """Convert Salesforce Cluster__c records into normalised cluster dicts.

        Only includes "real" clusters (those with Number_of_Nodes__c > 0,
        i.e. excluding Prism Central entries that report 0 nodes).

        Pulse status is determined by whether any ``_Pulse__c`` version
        fields are populated (the ``Last_Pulse__c`` timestamp is unreliable
        and only set on a small fraction of clusters).
        """
        clusters: list[dict[str, Any]] = []
        for rec in sf_clusters:
            nodes = int(rec.get("Number_of_Nodes__c") or 0)
            if nodes == 0:
                continue
            status = (rec.get("Cluster_Status__c") or "").lower()
            if status in ("terminated", "unlicensed", "never licensed", "hibernated"):
                continue

            pulse_declined = bool(rec.get("Pulse_Declined__c"))
            has_pulse_versions = any(
                rec.get(f) for f in NutanixInsightsConnector._PULSE_VERSION_FIELDS
            )
            pulse_active = (not pulse_declined) and has_pulse_versions

            aos = rec.get("Nutanix_core_version__c") or "unknown"
            hv_type = rec.get("Hypervisor_Type__c") or ""
            hv_ver = rec.get("Hypervisor_version__c") or ""
            hypervisor = f"{hv_type} {hv_ver}".strip() or ""

            lcm = rec.get("LCM_Version_Pulse__c") or ""
            foundation = rec.get("Foundation_Version_Pulse__c") or ""
            files = rec.get("AFS_Version_Pulse__c") or ""
            objects = rec.get("Buckets_version_Pulse__c") or ""
            calm = rec.get("Calm_version_Pulse__c") or ""
            era = rec.get("Era_Version_Pulse__c") or ""
            karbon = rec.get("Karbon_version_Pulse__c") or ""
            file_analytics = rec.get("File_Analytics_Version_Pulse__c") or ""

            clusters.append({
                "cluster_id": rec.get("Cluster_ID__c") or rec.get("Name") or rec.get("Id", ""),
                "cluster_name": rec.get("Cluster_Name__c") or rec.get("Name") or "",
                "node_count": nodes,
                "aos_version": aos,
                "hypervisor": hypervisor,
                "hypervisor_type": hv_type,
                "hypervisor_version": hv_ver,
                "pc_version": rec.get("PC_Version_Pulse__c"),
                "ncc_version": rec.get("NCC_Version_Pulse__c"),
                "lcm_version": lcm,
                "foundation_version": foundation,
                "files_version": files,
                "objects_version": objects,
                "hw_partner": rec.get("HW_Partner__c") or "",
                "hardware_models": [],
                "platforms": [],
                "contract_status": rec.get("Contract_Status__c") or "",
                "contract_end": rec.get("Support_End_Date__c"),
                "pulse_active": pulse_active,
                "last_pulse": rec.get("Last_Pulse__c") or "",
                "total_cores": 0,
                "memory_per_node": None,
                "total_ssd_tb": 0,
                "total_hdd_tb": 0,
                "compression": False,
                "dedup": False,
                "component_versions": {
                    k: v for k, v in {
                        "AOS": aos,
                        "Hypervisor": hypervisor or None,
                        "PC": rec.get("PC_Version_Pulse__c"),
                        "NCC": rec.get("NCC_Version_Pulse__c"),
                        "LCM": lcm or None,
                        "Foundation": foundation or None,
                        "Files": files or None,
                        "Objects": objects or None,
                        "Calm": calm or None,
                        "Era": era or None,
                        "Karbon": karbon or None,
                        "File Analytics": file_analytics or None,
                    }.items() if v
                },
                "coverage": rec.get("Support_Level__c") or "",
                "is_poc": bool(rec.get("Is_POC_Cluster__c")),
                "cloud_provider": rec.get("Cloud_Provider__c") or "",
            })

        clusters.sort(key=lambda c: c["node_count"], reverse=True)
        return clusters

    # ── Build per-version cluster stubs from accountportalvalues ────────

    _NOISE_VERSIONS = frozenset({"unknown", "InValidVersion", "master"})

    @classmethod
    def _clusters_from_portal_values(
        cls,
        aos_versions: dict[str, int],
        contract_status: str,
        pulse_enabled: bool,
    ) -> list[dict[str, Any]]:
        """
        Build cluster-like records from the AOS version distribution
        in accountportalvalues.  Each distinct AOS version becomes one
        logical cluster group (the portal doesn't expose per-cluster IDs).

        Non-standard entries (unknown, InValidVersion, master) are merged
        into a single "Unknown" group so they're still visible in the UI.
        """
        clusters: list[dict[str, Any]] = []
        unknown_count = 0

        for ver, count in aos_versions.items():
            if ver in cls._NOISE_VERSIONS:
                unknown_count += count
                continue
            clusters.append({
                "cluster_id": f"aos-{ver}",
                "node_count": count,
                "aos_version": ver,
                "hypervisor": "",
                "hypervisor_type": "",
                "pc_version": None,
                "ncc_version": None,
                "hardware_models": [],
                "platforms": [],
                "contract_status": str(contract_status),
                "contract_end": None,
                "pulse_active": pulse_enabled,
                "last_pulse": None,
                "total_cores": 0,
                "memory_per_node": None,
                "total_ssd_tb": 0,
                "total_hdd_tb": 0,
                "compression": False,
                "dedup": False,
                "component_versions": {},
                "coverage": "",
            })

        if unknown_count:
            clusters.append({
                "cluster_id": "aos-unknown",
                "node_count": unknown_count,
                "aos_version": "Unknown",
                "hypervisor": "",
                "hypervisor_type": "",
                "pc_version": None,
                "ncc_version": None,
                "hardware_models": [],
                "platforms": [],
                "contract_status": str(contract_status),
                "contract_end": None,
                "pulse_active": pulse_enabled,
                "last_pulse": None,
                "total_cores": 0,
                "memory_per_node": None,
                "total_ssd_tb": 0,
                "total_hdd_tb": 0,
                "compression": False,
                "dedup": False,
                "component_versions": {},
                "coverage": "",
            })

        clusters.sort(key=lambda c: (c["aos_version"] == "Unknown", c["aos_version"]))
        return clusters

    # ── EOL date parsing ────────────────────────────────────────────────

    _EOL_DATE_FORMATS = [
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%b %d, %Y",
        "%B %d, %Y",
        "%m/%d/%Y",
        "%d-%b-%Y",
    ]

    @classmethod
    def _parse_eol_date(cls, date_str: str) -> datetime | None:
        """Best-effort parse of various date formats from the EOL API."""
        if not date_str:
            return None
        date_str = date_str.strip()
        for fmt in cls._EOL_DATE_FORMATS:
            try:
                return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        try:
            ts = int(date_str) / 1000 if len(date_str) > 10 else int(date_str)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError):
            pass
        return None

    # ── Aggregated health summary ────────────────────────────────────────

    @staticmethod
    def _clusters_from_raw_api(
        raw_clusters: list[dict],
        contract_status: str,
    ) -> list[dict[str, Any]]:
        """
        Build rich cluster records from the raw /clusters API response.
        These have real cluster IDs, node counts, hypervisor info, pulse
        status, and hardware details — unlike the synthetic stubs from
        /accountportalvalues.
        """
        now = datetime.now(timezone.utc)
        clusters: list[dict[str, Any]] = []
        for c in raw_clusters:
            cid = (
                c.get("clusterUuid")
                or c.get("clusterName")
                or c.get("serialNumber")
                or c.get("id")
                or "unknown"
            )
            name = c.get("clusterName") or c.get("name") or ""
            display_id = name if name else str(cid)[:16]
            node_count = c.get("numNodes") or c.get("nodeCount") or 0
            aos = c.get("nosVersion") or c.get("aosVersion") or c.get("version") or "unknown"
            hv_type = c.get("hypervisorType") or c.get("hypervisor") or ""
            hv_ver = c.get("hypervisorVersion") or ""
            hypervisor = f"{hv_type} {hv_ver}".strip() or hv_type or ""
            pc = c.get("pcVersion") or c.get("prismCentralVersion") or None
            ncc = c.get("nccVersion") or None

            models = sorted({
                m for m in [c.get("modelName"), c.get("platform"), c.get("blockModel")]
                if m
            }) or []
            platforms = sorted({
                p for p in [c.get("platform"), c.get("modelFamily")]
                if p
            }) or []

            c_contract = c.get("contractStatus") or str(contract_status)
            contract_end = c.get("contractEndDate")

            pulse_flag = c.get("isPulseEnabled") or c.get("pulseEnabled") or False
            last_pulse_str = c.get("lastPulseDate") or c.get("lastPulseTime")
            pulse_active = bool(pulse_flag)
            if last_pulse_str and not pulse_flag:
                try:
                    lp = datetime.fromisoformat(
                        str(last_pulse_str).replace("+0000", "+00:00").replace("Z", "+00:00")
                    )
                    pulse_active = (now - lp).days < _PULSE_STALE_DAYS
                except (ValueError, TypeError):
                    pass

            clusters.append({
                "cluster_id": display_id,
                "cluster_uuid": str(cid),
                "node_count": node_count,
                "aos_version": aos,
                "hypervisor": hypervisor,
                "hypervisor_type": hv_type,
                "pc_version": pc,
                "ncc_version": ncc,
                "hardware_models": models,
                "platforms": platforms,
                "contract_status": c_contract,
                "contract_end": contract_end,
                "pulse_active": pulse_active,
                "last_pulse": last_pulse_str,
                "total_cores": c.get("totalCores") or c.get("cores") or 0,
                "memory_per_node": c.get("perNodeMemory") or None,
                "total_ssd_tb": round((c.get("ssd") or 0) / 1e12, 1) if c.get("ssd") else 0,
                "total_hdd_tb": round((c.get("hdd") or 0) / 1e12, 1) if c.get("hdd") else 0,
                "compression": bool(c.get("compressionEnabled")),
                "dedup": bool(c.get("dedupEnabled")),
                "component_versions": {},
                "coverage": c.get("coverage") or "",
            })

        clusters.sort(key=lambda cl: cl["aos_version"])
        return clusters

    @classmethod
    def build_health_summary_from_sf(
        cls,
        sf_clusters: list[dict[str, Any]],
        account_name: str,
        cached_globals: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """
        Build a health summary purely from Salesforce Cluster__c records,
        without any portal API calls.  Used when SF is live but the
        browser-based insights connector is unavailable.

        ``cached_globals`` is the output of
        :func:`app.sync.portal_globals_sync.load_cached_globals`. When
        present, it lets us still compute advisory matches and EOL
        exposure against the SF-derived cluster fingerprint, which is
        the main signal users miss when the browser bridge is offline.
        Pass ``None`` (or an empty dict) to keep the original behaviour
        of zero advisories / zero EOL exposure.
        """
        now = datetime.now(timezone.utc)
        stale = timedelta(days=_PULSE_STALE_DAYS)

        clusters = cls._clusters_from_sf(sf_clusters, now, stale)
        total_nodes = sum(c.get("node_count", 0) for c in clusters)

        pulse_connected = sum(1 for c in clusters if c.get("pulse_active"))
        pulse_disconnected = sum(1 for c in clusters if not c.get("pulse_active"))

        # Version / hardware distributions
        versions_summary: dict[str, dict[str, int]] = {
            "AOS": defaultdict(int),
            "Hypervisor": defaultdict(int),
            "PC": defaultdict(int),
            "NCC": defaultdict(int),
            "LCM": defaultdict(int),
            "Foundation": defaultdict(int),
            "Files": defaultdict(int),
            "Objects": defaultdict(int),
        }
        hardware_summary: dict[str, int] = defaultdict(int)
        hypervisor_distribution: dict[str, int] = defaultdict(int)
        ahv_versions: dict[str, int] = defaultdict(int)
        esxi_versions: dict[str, int] = defaultdict(int)
        hw_partner_dist: dict[str, int] = defaultdict(int)
        contract_statuses: dict[str, int] = defaultdict(int)

        for c in clusters:
            nc = c.get("node_count") or 1
            v = c.get("aos_version") or "Unknown"
            versions_summary["AOS"][v] += nc

            hv = c.get("hypervisor") or ""
            if hv:
                versions_summary["Hypervisor"][hv] += nc
            pc = c.get("pc_version")
            if pc:
                versions_summary["PC"][pc] += nc
            ncc = c.get("ncc_version")
            if ncc:
                versions_summary["NCC"][ncc] += nc
            lcm = c.get("lcm_version")
            if lcm:
                versions_summary["LCM"][lcm] += nc
            fnd = c.get("foundation_version")
            if fnd:
                versions_summary["Foundation"][fnd] += nc
            files = c.get("files_version")
            if files:
                versions_summary["Files"][files] += nc
            obj = c.get("objects_version")
            if obj:
                versions_summary["Objects"][obj] += nc

            for m in c.get("hardware_models", []):
                if m:
                    hardware_summary[m] += nc

            hv_type = (c.get("hypervisor_type") or hv.split()[0] if hv else "").upper()
            if hv_type in ("AHV", "ESXI"):
                hypervisor_distribution[hv_type] += nc
                hv_ver = c.get("hypervisor_version") or hv
                if hv_type == "AHV" and hv_ver:
                    ahv_versions[hv_ver] += nc
                elif hv_type == "ESXI" and hv_ver:
                    esxi_versions[hv_ver] += nc
            elif hv:
                hypervisor_distribution[hv_type or "Other"] += nc

            hw = c.get("hw_partner") or ""
            if hw:
                hw_partner_dist[hw] += nc

            cs = c.get("contract_status") or "Unknown"
            contract_statuses[cs] += 1

        display_aos = dict(versions_summary["AOS"])
        versions_out = {k: dict(v_) for k, v_ in versions_summary.items() if v_}
        hardware_out = dict(hardware_summary) if hardware_summary else dict(hw_partner_dist)

        top_contract = max(contract_statuses, key=contract_statuses.get) if contract_statuses else "unknown"

        # Enrichment from the global cache (security/field advisories, EOL).
        # Each block degrades gracefully — missing cache keys leave the
        # corresponding fields at their zero-baseline values.
        applicable_advisories: list[dict[str, Any]] = []
        provenance_used: list[str] = []

        if cached_globals:
            sec = cached_globals.get("security_advisories")
            if sec and isinstance(sec.get("payload"), dict):
                sec_list = sec["payload"].get("advisories") or []
                if isinstance(sec_list, list):
                    applicable_advisories.extend(
                        cls._match_advisories(sec_list, "security", clusters)
                    )
                    provenance_used.append(f"security:{sec.get('provenance', '?')}")

            field = cached_globals.get("field_advisories")
            if field and isinstance(field.get("payload"), dict):
                field_list = field["payload"].get("advisories") or []
                if isinstance(field_list, list):
                    applicable_advisories.extend(
                        cls._match_advisories(field_list, "field", clusters)
                    )
                    provenance_used.append(f"field:{field.get('provenance', '?')}")

        adv_critical = sum(
            1 for a in applicable_advisories if a["severity"].lower() == "critical"
        )
        adv_high = sum(
            1 for a in applicable_advisories if a["severity"].lower() == "high"
        )
        adv_medium = sum(
            1 for a in applicable_advisories if a["severity"].lower() == "medium"
        )

        eol_exposure = 0
        eol_versions_in_use: list[str] = []
        approaching_eol_count = 0
        approaching_eol_versions: list[str] = []
        if cached_globals:
            eol_blob = cached_globals.get("eol_versions")
            if eol_blob and isinstance(eol_blob.get("payload"), dict):
                eol_data = eol_blob["payload"].get("eol") or {}
                if isinstance(eol_data, dict):
                    (
                        eol_exposure,
                        eol_versions_in_use,
                        approaching_eol_count,
                        approaching_eol_versions,
                    ) = cls._compute_eol_exposure(eol_data, clusters)
                    if eol_exposure or approaching_eol_count:
                        provenance_used.append(f"eol:{eol_blob.get('provenance', '?')}")

        # Sort and de-dup advisories so security and field don't clash on number.
        applicable_advisories.sort(key=lambda a: (
            {"critical": 0, "high": 1, "medium": 2}.get(a["severity"].lower(), 3),
            a.get("date", ""),
        ))

        return {
            "account_name": account_name,
            "source": "salesforce+cache" if provenance_used else "salesforce",
            "source_provenance": provenance_used,
            "total_clusters": len(clusters),
            "total_nodes": total_nodes,
            "unknown_nodes": 0,
            "unhealthy_clusters": 0,
            "total_critical_alerts": 0,
            "total_warning_alerts": 0,
            "total_ncc_failures": 0,
            "contract_status": top_contract,
            "aos_versions": display_aos,
            "pulse_enabled": pulse_connected > 0,
            "pulse_connected": pulse_connected,
            "pulse_disconnected": pulse_disconnected,
            "eol_exposure_count": eol_exposure,
            "eol_versions_in_use": eol_versions_in_use,
            "approaching_eol_count": approaching_eol_count,
            "approaching_eol_versions": approaching_eol_versions,
            "cluster_warnings": [],
            "portal_open_cases": 0,
            "clusters": clusters,
            "versions_summary": versions_out,
            "hardware_summary": hardware_out,
            "hypervisor_distribution": dict(hypervisor_distribution),
            "ahv_versions": dict(ahv_versions),
            "esxi_versions": dict(esxi_versions),
            "hw_partner_distribution": dict(hw_partner_dist),
            "applicable_advisories": applicable_advisories,
            "advisory_stats": {
                "total": len(applicable_advisories),
                "critical": adv_critical,
                "high": adv_high,
                "medium": adv_medium,
            },
        }

    # ── EOL exposure helper (used by SF+cache fallback) ─────────────────

    @classmethod
    def _compute_eol_exposure(
        cls,
        eol_data: dict[str, Any],
        clusters: list[dict[str, Any]],
    ) -> tuple[int, list[str], int, list[str]]:
        """Return (exposure_nodes, eol_versions, approaching_nodes, approaching_versions).

        Mirrors the inline computation in :meth:`get_account_health_summary`
        so the SF+cache fallback produces the same shape of output.
        """
        now = datetime.now(timezone.utc)
        approaching_window = timedelta(days=180)

        eol_set: set[str] = set()
        approaching_set: set[str] = set()
        eol_entries = eol_data.get("aos", eol_data.get("acropolis", []))
        if isinstance(eol_entries, list):
            for entry in eol_entries:
                if isinstance(entry, dict):
                    v = entry.get("version", "")
                    eol_date_str = (
                        entry.get("eolDate")
                        or entry.get("endOfLifeDate")
                        or entry.get("eosl")
                        or entry.get("eoslDate")
                        or ""
                    )
                else:
                    v = str(entry)
                    eol_date_str = ""
                if not v:
                    continue
                eol_dt = cls._parse_eol_date(eol_date_str)
                if eol_dt:
                    if eol_dt <= now:
                        eol_set.add(v)
                    elif eol_dt <= now + approaching_window:
                        approaching_set.add(v)
                else:
                    eol_set.add(v)

        cluster_aos_counts: dict[str, int] = defaultdict(int)
        for c in clusters:
            ver = c.get("aos_version", "")
            if ver and ver not in ("Unknown", "unknown"):
                cluster_aos_counts[ver] += c.get("node_count", 0)

        eol_exposure = 0
        eol_versions_in_use: list[str] = []
        approaching_count = 0
        approaching_versions: list[str] = []
        for ver, count in cluster_aos_counts.items():
            if ver in eol_set:
                eol_exposure += count
                if ver not in eol_versions_in_use:
                    eol_versions_in_use.append(ver)
            elif ver in approaching_set:
                approaching_count += count
                if ver not in approaching_versions:
                    approaching_versions.append(ver)

        return eol_exposure, eol_versions_in_use, approaching_count, approaching_versions

    async def get_account_health_summary(
        self, account_name: str, account_id: str = "",
        sf_clusters: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """
        Build a rich health summary for one account.

        Strategy (priority order):
        1. Use Salesforce Cluster__c records (sf_clusters) — authoritative
           source with per-cluster details, node counts, AOS versions,
           and pulse timestamps.
        2. Supplement with /accountportalvalues for contract/pulse booleans.
        3. Fall back to synthetic per-version stubs when SF clusters are
           unavailable.
        """
        cluster_warnings = await self.get_cluster_warnings()

        portal_values: dict[str, Any] = {}
        if account_id:
            try:
                portal_values = await self.get_account_portal_values(account_id)
            except Exception as exc:
                logger.warning("Could not fetch portal values for %s: %s", account_id, exc)

        account_warnings = [
            w for w in cluster_warnings
            if account_name.lower() in str(w).lower()
        ]

        contract_status = portal_values.get("hasValidContract", "unknown")
        aos_versions: dict[str, int] = portal_values.get("assetAOSVersions") or {}
        pulse_enabled = portal_values.get("isPulseEnabled", False)

        # --- Build cluster list from Salesforce Cluster__c (primary) ---
        now = datetime.now(timezone.utc)
        pulse_stale = timedelta(days=_PULSE_STALE_DAYS)

        if sf_clusters:
            clusters = self._clusters_from_sf(sf_clusters, now, pulse_stale)
            # Override version map with real per-cluster AOS data
            sf_aos: dict[str, int] = defaultdict(int)
            for cl in clusters:
                ver = cl.get("aos_version") or "unknown"
                sf_aos[ver] += cl.get("node_count", 0)
            if sf_aos:
                aos_versions = dict(sf_aos)
        else:
            clusters = self._clusters_from_portal_values(
                aos_versions, contract_status, pulse_enabled,
            )

        real_cluster_count = len(clusters)
        total_nodes = sum(c.get("node_count", 0) for c in clusters)

        # Clean version map for display (exclude noise labels)
        clean_aos: dict[str, int] = {
            v: n for v, n in aos_versions.items()
            if v not in self._NOISE_VERSIONS
        }
        unknown_nodes = sum(
            n for v, n in aos_versions.items()
            if v in self._NOISE_VERSIONS
        )

        # --- Pulse counts ---
        pulse_connected = sum(1 for c in clusters if c.get("pulse_active"))
        pulse_disconnected = sum(1 for c in clusters if not c.get("pulse_active"))

        # --- EOL check with date-based approaching-EOL detection ---
        eol_data: dict = {}
        try:
            eol_data = await self.get_eol_versions()
        except Exception:
            pass

        now = datetime.now(timezone.utc)
        approaching_eol_window = timedelta(days=180)

        eol_exposure = 0
        eol_versions_in_use: list[str] = []
        approaching_eol_count = 0
        approaching_eol_versions: list[str] = []
        if eol_data:
            eol_entries = eol_data.get("aos", eol_data.get("acropolis", []))
            eol_set: set[str] = set()
            approaching_eol_set: set[str] = set()
            if isinstance(eol_entries, list):
                for entry in eol_entries:
                    if isinstance(entry, dict):
                        v = entry.get("version", "")
                        eol_date_str = (
                            entry.get("eolDate")
                            or entry.get("endOfLifeDate")
                            or entry.get("eosl")
                            or entry.get("eoslDate")
                            or ""
                        )
                    else:
                        v = str(entry)
                        eol_date_str = ""

                    if not v:
                        continue

                    eol_dt = self._parse_eol_date(eol_date_str)
                    if eol_dt:
                        if eol_dt <= now:
                            eol_set.add(v)
                        elif eol_dt <= now + approaching_eol_window:
                            approaching_eol_set.add(v)
                    else:
                        eol_set.add(v)

            # Build version→node-count map from clusters (real or stub)
            cluster_aos_counts: dict[str, int] = defaultdict(int)
            for c in clusters:
                ver = c.get("aos_version", "")
                if ver and ver not in ("Unknown", "unknown"):
                    cluster_aos_counts[ver] += c.get("node_count", 0)
            # Also include portal values versions not covered by clusters
            for ver, count in clean_aos.items():
                if ver not in cluster_aos_counts:
                    cluster_aos_counts[ver] = count

            for ver, count in cluster_aos_counts.items():
                if ver in eol_set:
                    eol_exposure += count
                    if ver not in eol_versions_in_use:
                        eol_versions_in_use.append(ver)
                elif ver in approaching_eol_set:
                    approaching_eol_count += count
                    if ver not in approaching_eol_versions:
                        approaching_eol_versions.append(ver)

        # --- Advisory matching ---
        sec_advisories: list[dict] = []
        field_advisories: list[dict] = []
        try:
            sec_advisories = await self.get_security_advisories()
        except Exception as exc:
            logger.warning("Could not fetch security advisories: %s", exc)
        try:
            field_advisories = await self.get_field_advisories()
        except Exception as exc:
            logger.warning("Could not fetch field advisories: %s", exc)

        applicable = (
            self._match_advisories(sec_advisories, "security", clusters)
            + self._match_advisories(field_advisories, "field", clusters)
        )

        adv_critical = sum(
            1 for a in applicable if a["severity"].lower() == "critical"
        )
        adv_high = sum(
            1 for a in applicable if a["severity"].lower() == "high"
        )
        adv_medium = sum(
            1 for a in applicable if a["severity"].lower() == "medium"
        )

        # --- Portal cases ---
        open_cases: list[dict] = []
        if account_id:
            try:
                open_cases = await self.get_open_cases(account_id)
            except Exception as exc:
                logger.warning("Could not fetch portal cases for %s: %s", account_id, exc)

        # Build version/hardware summaries from real cluster data when available
        hypervisor_distribution: dict[str, int] = defaultdict(int)
        ahv_versions_out: dict[str, int] = defaultdict(int)
        esxi_versions_out: dict[str, int] = defaultdict(int)
        hw_partner_dist: dict[str, int] = defaultdict(int)

        if sf_clusters and clusters:
            versions_summary: dict[str, dict[str, int]] = {
                "AOS": defaultdict(int),
                "Hypervisor": defaultdict(int),
                "PC": defaultdict(int),
                "NCC": defaultdict(int),
                "LCM": defaultdict(int),
                "Foundation": defaultdict(int),
                "Files": defaultdict(int),
                "Objects": defaultdict(int),
            }
            hardware_summary: dict[str, int] = defaultdict(int)
            for c in clusters:
                v = c.get("aos_version") or "Unknown"
                nc = c.get("node_count") or 1
                versions_summary["AOS"][v] += nc
                hv = c.get("hypervisor") or ""
                if hv:
                    versions_summary["Hypervisor"][hv] += nc
                pc = c.get("pc_version")
                if pc:
                    versions_summary["PC"][pc] += nc
                ncc = c.get("ncc_version")
                if ncc:
                    versions_summary["NCC"][ncc] += nc
                lcm = c.get("lcm_version")
                if lcm:
                    versions_summary["LCM"][lcm] += nc
                fnd = c.get("foundation_version")
                if fnd:
                    versions_summary["Foundation"][fnd] += nc
                files = c.get("files_version")
                if files:
                    versions_summary["Files"][files] += nc
                obj = c.get("objects_version")
                if obj:
                    versions_summary["Objects"][obj] += nc

                for m in c.get("hardware_models", []):
                    if m:
                        hardware_summary[m] += nc

                hv_type = (c.get("hypervisor_type") or c.get("hypervisor", "").split()[0] or "").upper()
                if hv_type in ("AHV", "ESXI"):
                    hypervisor_distribution[hv_type] += nc
                    hv_ver = c.get("hypervisor_version") or c.get("hypervisor") or ""
                    if hv_type == "AHV" and hv_ver:
                        ahv_versions_out[hv_ver] += nc
                    elif hv_type == "ESXI" and hv_ver:
                        esxi_versions_out[hv_ver] += nc
                elif hv:
                    hypervisor_distribution[hv_type or "Other"] += nc

                hw = c.get("hw_partner") or ""
                if hw:
                    hw_partner_dist[hw] += nc

            display_aos = dict(versions_summary["AOS"])
            versions_out = {k: dict(v) for k, v in versions_summary.items() if v}
            hardware_out = dict(hardware_summary) if hardware_summary else dict(hw_partner_dist)
        else:
            display_aos = dict(clean_aos)
            if unknown_nodes:
                display_aos["Unknown"] = unknown_nodes
            versions_out = {"AOS": display_aos}
            hardware_out = {}

        return {
            "account_name": account_name,
            "source": "portal.example.com",
            "total_clusters": real_cluster_count,
            "total_nodes": total_nodes,
            "unknown_nodes": unknown_nodes,
            "unhealthy_clusters": len(account_warnings),
            "total_critical_alerts": len(account_warnings),
            "total_warning_alerts": 0,
            "total_ncc_failures": 0,
            "contract_status": contract_status,
            "aos_versions": display_aos,
            "pulse_enabled": pulse_enabled,
            "pulse_connected": pulse_connected,
            "pulse_disconnected": pulse_disconnected,
            "eol_exposure_count": eol_exposure,
            "eol_versions_in_use": eol_versions_in_use,
            "approaching_eol_count": approaching_eol_count,
            "approaching_eol_versions": approaching_eol_versions,
            "cluster_warnings": account_warnings,
            "portal_open_cases": len(open_cases),
            "clusters": clusters,
            "versions_summary": versions_out,
            "hardware_summary": hardware_out,
            "hypervisor_distribution": dict(hypervisor_distribution),
            "ahv_versions": dict(ahv_versions_out),
            "esxi_versions": dict(esxi_versions_out),
            "hw_partner_distribution": dict(hw_partner_dist),
            "applicable_advisories": applicable,
            "advisory_stats": {
                "total": len(applicable),
                "critical": adv_critical,
                "high": adv_high,
                "medium": adv_medium,
            },
        }
