"""
Realistic demo data for the Adoption & Escalation Risk Analyzer.
Used when live connectors are unavailable (no credentials / proxy / network).

PUBLIC-DEMO SANITIZED VERSION
-----------------------------
This is the publish-safe variant of the internal demo_data.py:
  * All company names are FICTIONAL (no real customers).
  * The internal author username has been replaced with "demo.user".
  * Structure, function signatures, and response shapes are byte-for-byte
    compatible with the original so the app behaves identically.

Drop this in as app/connectors/demo_data.py in the public copy.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any

_NOW = datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# Fictional organisations only — these do not represent any real company.
DEMO_ACCOUNTS: list[dict[str, str]] = [
    {"id": "001Dn00000A1bCdEF", "name": "Meridian Capital Bank"},
    {"id": "001Dn00000B2cDeGH", "name": "Helios Consulting Services"},
    {"id": "001Dn00000C3dEfIJ", "name": "Vanguard Industries"},
    {"id": "001Dn00000D4eFgKL", "name": "Northwind Software"},
    {"id": "001Dn00000E5fGhMN", "name": "Cobalt Technologies"},
    {"id": "001Dn00000F6gHiOP", "name": "Quantum Telecom"},
    {"id": "001Dn00000G7hIjQR", "name": "Sterling National Bank"},
    {"id": "001Dn00000H8iJkST", "name": "Crestline Bank"},
    {"id": "001Dn00000I9jKlUV", "name": "Ironclad Motors"},
    {"id": "001Dn00000J0kLmWX", "name": "Apex Engineering & Construction"},
    {"id": "001Dn00000K1lMnYZ", "name": "Lumen Tech Solutions"},
    {"id": "001Dn00000L2mNoAB", "name": "Vertex Digital"},
    {"id": "001Dn00000M3nOpCD", "name": "Pinnacle Bank"},
    {"id": "001Dn00000N4oPqEF", "name": "Solace Financial"},
    {"id": "001Dn00000O5pQrGH", "name": "Aurora Enterprises"},
]

SUBJECTS = [
    "Cluster unreachable after AOS upgrade",
    "VM migration failing between containers",
    "NCC health check reporting disk errors",
    "Prism Central SSO authentication loop",
    "Data protection snapshot schedule missed",
    "AHV host CVM not responding to heartbeat",
    "Storage pool showing degraded resilience",
    "ERA database provisioning timeout",
    "Flow microsegmentation policy not applying",
    "Calm blueprint deployment stuck",
    "LCM firmware update failed mid-flight",
    "Objects store S3 API 503 errors",
    "Files share inaccessible from domain clients",
    "Metro availability failover did not trigger",
    "Xi Leap DR runbook incomplete replication",
    "Prism alerts flooding — disk SMART warnings",
    "AHV network bonding lost after reboot",
    "vCenter plugin not syncing VM inventory",
    "Foundation imaging failure on new nodes",
    "Karbon Kubernetes pod scheduling errors",
]

PRIORITIES = ["P1", "P1", "P2", "P2", "P2", "P3", "P3", "P3", "P3", "P4"]
STATUSES = ["New", "In Progress", "Waiting on Customer", "Escalated", "In Progress"]
OWNERS = [
    "Rajesh Kumar", "Priya Sharma", "Amit Patel", "Sneha Reddy",
    "Vikram Singh", "Ananya Gupta", "Karthik Nair", "Deepa Iyer",
]


def _make_case(account_id: str, account_name: str, idx: int) -> dict[str, Any]:
    priority = random.choice(PRIORITIES)
    days_ago = random.randint(1, 60)
    created = _NOW - timedelta(days=days_ago)
    is_escalated = random.random() < 0.3
    status = "Escalated" if is_escalated else random.choice(STATUSES)
    is_closed = status.startswith("Closed")

    return {
        "Id": f"500Dn0000{idx:06d}",
        "CaseNumber": f"{80000 + idx:08d}",
        "AccountId": account_id,
        "Account": {"Name": account_name},
        "Subject": random.choice(SUBJECTS),
        "Description": "Customer reported issue with their HCI environment.",
        "Priority": priority,
        "Status": status,
        "IsClosed": is_closed,
        "Origin": random.choice(["Phone", "Web", "Email"]),
        "Type": random.choice(["Problem", "Incident", "Question"]),
        "IsEscalated": is_escalated,
        "Reason": "Performance" if priority in ("P1", "P2") else "Functionality",
        "CreatedDate": _iso(created),
        "ClosedDate": None,
        "ContactId": f"003Dn0000{idx:06d}",
        "Contact": {"Name": f"Contact {idx}", "Email": f"contact{idx}@example.com"},
        "OwnerId": f"005Dn0000{idx % 8:06d}",
        "Owner": {"Name": random.choice(OWNERS)},
    }


def generate_sf_account_summary(account_id: str, account_name: str) -> dict[str, Any]:
    random.seed(hash(account_id) % 2**32)
    num_cases = random.randint(3, 25)
    cases = [_make_case(account_id, account_name, i + hash(account_id) % 10000) for i in range(num_cases)]
    open_cases = [c for c in cases if not c.get("IsClosed", False)]
    open_count = len(open_cases)
    escalated = sum(1 for c in open_cases if c["IsEscalated"])
    p1 = sum(1 for c in open_cases if c["Priority"] == "P1")
    p2 = sum(1 for c in open_cases if c["Priority"] == "P2")
    return {
        "account_id": account_id,
        "total_cases": num_cases,
        "open_cases": open_count,
        "escalated_cases": escalated,
        "p1_cases": p1,
        "p2_cases": p2,
        "cases": cases,
    }


_AOS_VERSIONS = ["5.17", "5.20.5", "6.5.6", "6.7.1.5", "6.8.1", "6.8.1.5", "6.10"]
_HV_TYPES = ["AHV", "ESXi", "AHV", "AHV"]
_AHV_VERSIONS = ["20220304.242", "20230302.101", "20240304.100"]
_ESXI_VERSIONS = ["VMware ESXi 7.0.0 build-15843807", "VMware ESXi 8.0u3"]
_MODELS = ["NX-3060-G9", "NX-1065-G5", "NX-3060-G7", "NX-8150-G9", "HPE DX365 Gen11", "Dell XC750"]
_PC_VERSIONS = ["pc.2024.1", "pc.2024.2", "pc.2023.4", None]
_NCC_VERSIONS = ["4.6.6", "4.6.5", "4.5.0.1", "3.10.0.6"]
_COVERAGES = [
    "Production Support: Next Business Day",
    "Production Support: 4-Hour Parts Delivery",
    "Premium Support: 24x7",
    "",
]


def _build_demo_advisories(clusters: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build demo advisories with affected_clusters details matched against actual demo cluster data."""
    g6g7_clusters = [
        {"cluster_id": c["cluster_id"], "aos_version": c["aos_version"],
         "node_count": c["node_count"], "hardware_models": c["hardware_models"],
         "hypervisor": c.get("hypervisor", "")}
        for c in clusters
        if any("G7" in m or "G6" in m for m in c.get("hardware_models", []))
    ]
    pre_681_clusters = [
        {"cluster_id": c["cluster_id"], "aos_version": c["aos_version"],
         "node_count": c["node_count"], "hardware_models": c["hardware_models"],
         "hypervisor": c.get("hypervisor", "")}
        for c in clusters
        if c.get("aos_version", "9") < "6.8.1" and c.get("aos_version") not in ("Unknown", "unknown")
    ]
    esxi_hpe_clusters = [
        {"cluster_id": c["cluster_id"], "aos_version": c["aos_version"],
         "node_count": c["node_count"], "hardware_models": c["hardware_models"],
         "hypervisor": c.get("hypervisor", "")}
        for c in clusters
        if "ESXi" in (c.get("hypervisor") or "") and any("DX365" in m or "DX385" in m for m in c.get("hardware_models", []))
    ]
    g9_clusters = [
        {"cluster_id": c["cluster_id"], "aos_version": c["aos_version"],
         "node_count": c["node_count"], "hardware_models": c["hardware_models"],
         "hypervisor": c.get("hypervisor", "")}
        for c in clusters
        if any("G9" in m for m in c.get("hardware_models", []) + c.get("platforms", []))
    ]

    sec = [
        {
            "type": "security", "number": "0030", "severity": "Critical",
            "title": "Security Advisory #0030 — BMC IPMI firmware vulnerability",
            "date": "Oct 15, 2023",
            "affected_versions": "All G6/G7 platforms with BMC firmware prior to 3.72.09",
            "summary": "BMC IPMI firmware vulnerability may allow remote code execution. Update BMC firmware to the latest version.",
            "href": "",
            "affected_cluster_count": len(g6g7_clusters),
            "affected_clusters": g6g7_clusters,
            "match_reasons": ["Platform generation G6/G7 matches"] if g6g7_clusters else [],
        },
        {
            "type": "security", "number": "0044", "severity": "High",
            "title": "Security Advisory #0044 — AOS OpenSSL vulnerability",
            "date": "Mar 10, 2025",
            "affected_versions": "AOS versions prior to 6.8.1",
            "summary": "OpenSSL CVE — upgrade AOS to 6.8.1 or later.",
            "href": "",
            "affected_cluster_count": len(pre_681_clusters),
            "affected_clusters": pre_681_clusters,
            "match_reasons": ["AOS version prior to 6.8.1 in affected range"] if pre_681_clusters else [],
        },
    ]

    fa = [
        {
            "type": "field", "number": "0128", "severity": "Critical",
            "title": "Field Advisory #0128 — CVM instability on ESXi 8.0u3 with NVMe hot-add",
            "date": "Aug 14, 2025",
            "affected_versions": "HPE DX365 Gen11, HPE DX385 Gen11 running ESXi 8.0u3 with AOS 6.8+",
            "summary": "Clusters may experience CVM instability during NVMe hot-addition workflow. Apply AOS patch 6.8.1.5 or later.",
            "href": "",
            "affected_cluster_count": len(esxi_hpe_clusters),
            "affected_clusters": esxi_hpe_clusters,
            "match_reasons": ["ESXi hypervisor affected", "Hardware HPE DX365 matches"] if esxi_hpe_clusters else [],
        },
        {
            "type": "field", "number": "0115", "severity": "Medium",
            "title": "Field Advisory #0115 — NCC false-positive disk alerts on G9 platforms",
            "date": "Jan 22, 2025",
            "affected_versions": "NCC versions prior to 4.6.5 on G9 platforms",
            "summary": "NCC may report false positive disk failure alerts. Upgrade NCC to 4.6.5+.",
            "href": "",
            "affected_cluster_count": len(g9_clusters),
            "affected_clusters": g9_clusters,
            "match_reasons": ["Platform generation G9 matches"] if g9_clusters else [],
        },
    ]

    return sec, fa


def generate_insights_health(account_name: str) -> dict[str, Any]:
    random.seed(hash(account_name) % 2**32)
    num_clusters = random.randint(2, 8)
    clusters: list[dict[str, Any]] = []
    total_crit = 0
    total_warn = 0
    total_ncc = 0
    unhealthy = 0
    versions_agg: dict[str, dict[str, int]] = {
        "AOS": {}, "Hypervisor": {}, "PC": {}, "NCC": {},
    }
    hardware_agg: dict[str, int] = {}
    eol_exposure = 0
    eol_versions_in_use: list[str] = []
    approaching_eol_count = 0
    approaching_eol_versions: list[str] = []
    pulse_connected = 0
    pulse_disconnected = 0

    for i in range(num_clusters):
        crit = random.randint(0, 4)
        warn = random.randint(0, 8)
        ncc_fail = random.randint(0, 6)
        health = random.choice(["healthy", "healthy", "healthy", "warning", "critical"])
        if health != "healthy":
            unhealthy += 1
        total_crit += crit
        total_warn += warn
        total_ncc += ncc_fail

        node_count = random.randint(2, 8)
        aos = random.choice(_AOS_VERSIONS)
        hv_type = random.choice(_HV_TYPES)
        if hv_type == "ESXi":
            hv_ver = random.choice(_ESXI_VERSIONS)
        else:
            hv_ver = f"AHV {random.choice(_AHV_VERSIONS)}"
        pc = random.choice(_PC_VERSIONS)
        ncc = random.choice(_NCC_VERSIONS)
        model = random.choice(_MODELS)
        contract_status = random.choice(["Active", "Active", "Active", "Expired"])
        pulse_active = random.choice([True, True, True, False])
        days_ago = random.randint(0, 60)
        last_pulse = _iso(_NOW - timedelta(days=days_ago))
        contract_end = (_NOW + timedelta(days=random.randint(-90, 730))).strftime("%Y-%m-%d")
        total_cores = node_count * random.choice([32, 48, 64, 128])
        mem = random.choice(["128GB", "256GB", "512GB", "768GB"])
        ssd_tb = round(node_count * random.uniform(1.5, 8.0), 1)
        hdd_tb = round(node_count * random.uniform(0, 20.0), 1)

        if pulse_active:
            pulse_connected += 1
        else:
            pulse_disconnected += 1

        if aos in ("5.17", "5.20.5"):
            eol_exposure += node_count
            if aos not in eol_versions_in_use:
                eol_versions_in_use.append(aos)
        elif aos in ("6.5.6",):
            approaching_eol_count += node_count
            if aos not in approaching_eol_versions:
                approaching_eol_versions.append(aos)

        versions_agg["AOS"][aos] = versions_agg["AOS"].get(aos, 0) + node_count
        versions_agg["Hypervisor"][hv_ver] = versions_agg["Hypervisor"].get(hv_ver, 0) + node_count
        if pc:
            versions_agg["PC"][pc] = versions_agg["PC"].get(pc, 0) + node_count
        versions_agg["NCC"][ncc] = versions_agg["NCC"].get(ncc, 0) + node_count
        hardware_agg[model] = hardware_agg.get(model, 0) + node_count

        clusters.append({
            "cluster_id": f"clu-{hash(account_name + str(i)) % 999999:06x}",
            "node_count": node_count,
            "aos_version": aos,
            "hypervisor": hv_ver,
            "hypervisor_type": hv_type,
            "pc_version": pc,
            "ncc_version": ncc,
            "hardware_models": [model],
            "platforms": [model],
            "contract_status": contract_status,
            "contract_end": contract_end,
            "pulse_active": pulse_active,
            "last_pulse": last_pulse,
            "total_cores": total_cores,
            "memory_per_node": mem,
            "total_ssd_tb": ssd_tb,
            "total_hdd_tb": hdd_tb,
            "compression": random.choice([True, False]),
            "dedup": random.choice([True, False]),
            "component_versions": {
                "Foundation": f"5.{random.randint(4,6)}.{random.randint(0,3)}",
                "LCM": f"2.{random.randint(5,7)}.0.{random.randint(0,5)}",
            },
            "coverage": random.choice(_COVERAGES),
        })

    total_nodes = sum(c["node_count"] for c in clusters)
    sec_advisories, field_advisories = _build_demo_advisories(clusters)
    applicable_advisories = sec_advisories + field_advisories
    applicable_advisories = [a for a in applicable_advisories if a["affected_cluster_count"] > 0]
    adv_crit = sum(1 for a in applicable_advisories if a["severity"] == "Critical")
    adv_high = sum(1 for a in applicable_advisories if a["severity"] == "High")
    adv_med = sum(1 for a in applicable_advisories if a["severity"] == "Medium")

    return {
        "account_name": account_name,
        "source": "demo",
        "total_clusters": num_clusters,
        "total_nodes": total_nodes,
        "unhealthy_clusters": unhealthy,
        "total_critical_alerts": total_crit,
        "total_warning_alerts": total_warn,
        "total_ncc_failures": total_ncc,
        "contract_status": "Active" if random.random() > 0.3 else "Expired",
        "aos_versions": versions_agg.get("AOS", {}),
        "pulse_enabled": pulse_connected > 0,
        "pulse_connected": pulse_connected,
        "pulse_disconnected": pulse_disconnected,
        "eol_exposure_count": eol_exposure,
        "eol_versions_in_use": eol_versions_in_use,
        "approaching_eol_count": approaching_eol_count,
        "approaching_eol_versions": approaching_eol_versions,
        "cluster_warnings": [],
        "portal_open_cases": random.randint(0, 5),
        "clusters": clusters,
        "versions_summary": versions_agg,
        "hardware_summary": hardware_agg,
        "applicable_advisories": applicable_advisories,
        "advisory_stats": {
            "total": len(applicable_advisories),
            "critical": adv_crit,
            "high": adv_high,
            "medium": adv_med,
        },
    }


def generate_cs_summary(account_id: str) -> dict[str, Any]:
    random.seed(hash(account_id + "cs") % 2**32)
    health_score = random.randint(20, 95)
    engagement = random.randint(15, 90)
    adoption = random.randint(25, 92)
    nps = random.randint(-20, 80)
    days_to_renewal = random.choice([30, 60, 90, 120, 180, 270, 365])
    utilization = random.randint(40, 98)
    renewal_risk_score = random.randint(10, 95)
    contract_value = random.choice([250000, 500000, 1200000, 3500000, 8000000])

    if nps < 0:
        sentiment = "negative"
    elif nps < 30:
        sentiment = "neutral"
    else:
        sentiment = "positive"

    if health_score < 40:
        trend = "declining"
    elif health_score < 60:
        trend = "stable"
    else:
        trend = "improving"

    risk_labels = ["On Track", "On Track", "On Track", "Internal Processes",
                   "Potential Slip", "Poor Adoption", "Competition",
                   "Economic Health / Budget", "No Communication"]
    renewal_risk_label = random.choice(risk_labels)

    if renewal_risk_score >= 70:
        renewal_risk = "high"
    elif renewal_risk_score >= 40:
        renewal_risk = "medium"
    else:
        renewal_risk = "low"

    signals = []
    if health_score < 40:
        signals.append("Low health score trend")
    if engagement < 30:
        signals.append("Declining engagement")
    if nps < 0:
        signals.append("Detractor NPS")
    if utilization < 50:
        signals.append("Low license utilization")
    if renewal_risk_score >= 70:
        signals.append(f"High renewal risk score: {renewal_risk_score}%")

    demo_products = [
        {"product": "NCI Ultimate", "capacity": 100, "purchased": 100, "activated": int(100 * adoption / 100), "adoption_pct": adoption},
        {"product": "NCM Pro", "capacity": 50, "purchased": 50, "activated": int(50 * max(0, adoption - 20) / 100), "adoption_pct": max(0, adoption - 20)},
        {"product": "NDB Starter", "capacity": 20, "purchased": 20, "activated": int(20 * max(0, adoption - 40) / 100), "adoption_pct": max(0, adoption - 40)},
    ]

    return {
        "account_id": account_id,
        "health_score": health_score,
        "health_source": "estimated",
        "health_trend": trend,
        "adoption_score": adoption,
        "adoption_source": "estimated",
        "license_utilization_pct": utilization,
        "adoption_products": demo_products,
        "total_capacity": 170,
        "total_activated": sum(p["activated"] for p in demo_products),
        "engagement_score": engagement,
        "engagement_source": "estimated",
        "nps_score": nps,
        "sentiment_label": sentiment,
        "sentiment_source": "estimated",
        "renewal_date": _iso(_NOW + timedelta(days=days_to_renewal)),
        "contract_value": contract_value,
        "total_renewal_value": contract_value,
        "days_to_renewal": days_to_renewal,
        "renewal_risk": renewal_risk,
        "renewal_risk_score": renewal_risk_score,
        "renewal_risk_label": renewal_risk_label,
        "renewal_source": "estimated",
        "renewal_count": random.randint(1, 4),
        "renewals": [],
        "cs_risk_signals": signals,
        "raw": {},
    }


_ENGAGEMENT_SOURCES = [
    ("Tasks", 35), ("Meetings", 25), ("Internal Comments", 40),
]

_ENGAGEMENT_TITLES = [
    "Account review call — {name}",
    "Weekly escalation tracker — {name} issues",
    "{name} infrastructure roadmap sync",
    "TAM sync: {name} cluster expansion",
    "{name} renewal planning — Q3 strategy",
    "Support retrospective — {name} P1 incidents",
    "{name} AOS upgrade path recommendation",
    "Customer success check-in: {name}",
    "Internal note: {name} contract expiry approaching",
    "{name} POC evaluation feedback",
    "Architecture review for {name} DR setup",
    "{name} license optimization proposal",
    "Executive briefing: {name} account health",
    "Troubleshooting note — {name} NCC failures",
    "{name} onboarding call",
]


def generate_engagement_summary(account_id: str, account_name: str) -> dict[str, Any]:
    random.seed(hash(account_id + "engagement") % 2**32)
    total_mentions = random.randint(2, 85)
    recent_30d = max(0, int(total_mentions * random.uniform(0.1, 0.5)))
    escalation_mentions = random.randint(0, max(1, total_mentions // 8))
    comment_count = random.randint(0, max(1, total_mentions // 3))

    if total_mentions == 0:
        trend = "none"
    elif recent_30d >= total_mentions * 0.4:
        trend = "increasing"
    elif recent_30d >= total_mentions * 0.15:
        trend = "stable"
    else:
        trend = "declining"

    sources = []
    remaining = total_mentions
    for src_name, weight in _ENGAGEMENT_SOURCES:
        count = min(remaining, max(0, int(total_mentions * weight / 100 * random.uniform(0.5, 1.5))))
        if count > 0:
            sources.append({"source": src_name, "count": count})
            remaining -= count
        if remaining <= 0:
            break

    last_mentioned = _iso(_NOW - timedelta(days=random.randint(0, 30))) if total_mentions > 0 else None

    top_results = []
    for i in range(min(5, total_mentions)):
        title_template = random.choice(_ENGAGEMENT_TITLES)
        src = random.choice(_ENGAGEMENT_SOURCES)[0]
        days_ago = random.randint(1, 90)
        top_results.append({
            "title": title_template.format(name=account_name),
            "snippet": f"Internal activity related to {account_name} engagement and technical status...",
            "source": src,
            "author": random.choice(OWNERS),
            "date": _iso(_NOW - timedelta(days=days_ago)),
            "url": "",
            "type": random.choice(["task", "event", "comment"]),
        })

    signals = []
    if total_mentions < 5:
        signals.append(f"Only {total_mentions} internal touchpoints — low engagement")
    if recent_30d == 0 and total_mentions > 0:
        signals.append("No activity in the last 30 days — engagement may have stalled")
    if trend == "declining":
        signals.append("Internal activity trend is declining")
    if escalation_mentions > 0:
        signals.append(f"{escalation_mentions} escalated/P1-P2 case(s) in the last 180 days")
    if comment_count == 0 and total_mentions > 5:
        signals.append("No internal case comments — documentation gap")

    task_count = int(total_mentions * 0.35)
    event_count = int(total_mentions * 0.25)

    return {
        "account_id": account_id,
        "account_name": account_name,
        "source": "salesforce_engagement",
        "total_mentions": total_mentions,
        "recent_mentions_30d": recent_30d,
        "escalation_mentions": escalation_mentions,
        "knowledge_articles": comment_count,
        "top_sources": sources,
        "last_mentioned": last_mentioned,
        "mention_trend": trend,
        "top_results": top_results,
        "glean_risk_signals": signals,
        "unique_contributors": random.randint(1, 12),
        "task_count": task_count,
        "event_count": event_count,
        "comment_count": comment_count,
    }


DEMO_CONTACTS = [
    {"title": "VP Infrastructure", "dept": "IT"},
    {"title": "Director of Cloud Operations", "dept": "IT"},
    {"title": "Senior Systems Engineer", "dept": "Infrastructure"},
    {"title": "IT Manager", "dept": "IT"},
    {"title": "CTO", "dept": "Executive"},
    {"title": "Cloud Platform Architect", "dept": "Engineering"},
    {"title": "Head of Data Center Operations", "dept": "IT"},
    {"title": "Infrastructure Lead", "dept": "IT"},
]

FIRST_NAMES = ["Rahul", "Priya", "Vikram", "Anjali", "Suresh", "Deepa", "Arjun", "Meera"]
LAST_NAMES = ["Sharma", "Patel", "Kumar", "Singh", "Reddy", "Nair", "Gupta", "Iyer"]


def generate_contacts(account_id: str, account_name: str) -> list[dict[str, Any]]:
    random.seed(hash(account_id + "contacts") % 2**32)
    num = random.randint(3, 6)
    contacts = []
    domain = account_name.lower().split()[0].replace("&", "").replace("'", "") + ".example.com"

    primary_kw = {"vp", "cto", "director", "head"}
    used_names: set[str] = set()

    for i in range(num):
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        full = f"{first} {last}"
        if full in used_names:
            first = random.choice(FIRST_NAMES)
            full = f"{first} {last}"
        used_names.add(full)

        if i == 0:
            info = DEMO_CONTACTS[0]
        else:
            info = random.choice(DEMO_CONTACTS)
        email = f"{first.lower()}.{last.lower()}@{domain}"
        is_primary = any(t in info["title"].lower() for t in primary_kw)

        contacts.append({
            "Id": f"003Dn000{hash(account_id + str(i)) % 999999:06d}",
            "Name": full,
            "Email": email,
            "Title": info["title"],
            "Department": info["dept"],
            "Phone": f"+91-{random.randint(70000, 99999)}-{random.randint(10000, 99999)}",
            "is_primary": is_primary,
            "CreatedDate": _iso(_NOW - timedelta(days=random.randint(30, 730))),
        })

    contacts.sort(key=lambda c: (not c["is_primary"], c.get("CreatedDate", "")))
    return contacts


# Custom CXM risk flags — generic CS-risk patterns attached to fictional
# accounts, authored by a generic demo user (no real internal username).
_DEMO_CXM_FLAGS: list[dict[str, Any]] = [
    {
        "account_id": "001Dn00000A1bCdEF",
        "category": "champion_loss",
        "severity": "critical",
        "title": "Key infrastructure sponsor departing in Q3",
        "notes": "Primary technical champion is reportedly leaving. No successor identified yet.",
        "created_by": "demo.user",
    },
    {
        "account_id": "001Dn00000A1bCdEF",
        "category": "competitive_threat",
        "severity": "high",
        "title": "Competitive evaluation underway",
        "notes": "Customer running a competing platform POC in parallel for DR workloads.",
        "created_by": "demo.user",
    },
    {
        "account_id": "001Dn00000B2cDeGH",
        "category": "budget_pressure",
        "severity": "high",
        "title": "IT budget cut by 15% for next fiscal year",
        "notes": "Finance mandated across-the-board IT spending reduction.",
        "created_by": "demo.user",
    },
    {
        "account_id": "001Dn00000C3dEfIJ",
        "category": "low_feature_adoption",
        "severity": "medium",
        "title": "Add-on licenses unused",
        "notes": "Customer purchased add-on modules but the infra team has not started deployment.",
        "created_by": "demo.user",
    },
    {
        "account_id": "001Dn00000D4eFgKL",
        "category": "single_thread",
        "severity": "high",
        "title": "Only one contact engaged across entire account",
        "notes": "All interactions go through a single infra manager. Need multi-threading urgently.",
        "created_by": "demo.user",
    },
    {
        "account_id": "001Dn00000G7hIjQR",
        "category": "upgrade_blocked",
        "severity": "critical",
        "title": "Platform upgrade blocked due to change freeze",
        "notes": "A regulatory audit requires a change freeze this quarter. Clusters running EOL software.",
        "created_by": "demo.user",
    },
    {
        "account_id": "001Dn00000H8iJkST",
        "category": "ma_activity",
        "severity": "medium",
        "title": "Potential merger activity",
        "notes": "Possible M&A activity could affect infra consolidation decisions.",
        "created_by": "demo.user",
    },
]


def seed_demo_custom_risks() -> int:
    """Populate the custom risk store with sample flags for demo accounts.
    Returns the number of flags seeded."""
    from app.store.custom_risks import CustomRiskStore
    store = CustomRiskStore()
    seeded = 0
    for flag in _DEMO_CXM_FLAGS:
        existing = store.list_for_account(flag["account_id"])
        already = any(
            e.get("category") == flag["category"] and e.get("title") == flag["title"]
            for e in existing
        )
        if not already:
            store.add(
                account_id=flag["account_id"],
                category=flag["category"],
                severity=flag["severity"],
                title=flag["title"],
                notes=flag["notes"],
                created_by=flag["created_by"],
            )
            seeded += 1
    return seeded


def get_demo_escalated_cases() -> list[dict[str, Any]]:
    """Simulate the SF escalated cases query result."""
    all_cases = []
    for acc in DEMO_ACCOUNTS:
        summary = generate_sf_account_summary(acc["id"], acc["name"])
        for c in summary["cases"]:
            if c["IsEscalated"]:
                all_cases.append(c)
    return all_cases


def get_demo_open_cases() -> list[dict[str, Any]]:
    all_cases = []
    for acc in DEMO_ACCOUNTS:
        summary = generate_sf_account_summary(acc["id"], acc["name"])
        all_cases.extend(summary["cases"])
    return all_cases
