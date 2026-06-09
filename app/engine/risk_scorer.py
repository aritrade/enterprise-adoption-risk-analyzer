"""
Risk scoring engine — combines signals from Salesforce cases,
Nutanix Insights cluster health, and CS Insights customer-success
metrics into a unified escalation-risk score per account.

Score range: 0 (no risk) → 100 (critical escalation imminent).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class RiskFactor:
    source: str          # salesforce | insights | csinsights
    category: str        # e.g. "case_volume", "cluster_health"
    description: str
    weight: float        # contribution to final score (0-1)
    raw_score: float     # score before weighting (0-100)

    @property
    def weighted_score(self) -> float:
        return self.weight * self.raw_score


@dataclass
class AccountRiskProfile:
    account_id: str
    account_name: str
    overall_score: float = 0.0
    risk_level: str = "low"
    factors: list[RiskFactor] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    sf_summary: dict[str, Any] = field(default_factory=dict)
    insights_summary: dict[str, Any] = field(default_factory=dict)
    cs_summary: dict[str, Any] = field(default_factory=dict)
    glean_summary: dict[str, Any] = field(default_factory=dict)
    planhat_summary: dict[str, Any] = field(default_factory=dict)
    computed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "account_name": self.account_name,
            "overall_score": round(self.overall_score, 1),
            "risk_level": self.risk_level,
            "factors": [
                {
                    "source": f.source,
                    "category": f.category,
                    "description": f.description,
                    "weight": f.weight,
                    "raw_score": round(f.raw_score, 1),
                    "weighted_score": round(f.weighted_score, 1),
                }
                for f in self.factors
            ],
            "recommendations": self.recommendations,
            "salesforce": self.sf_summary,
            "insights": self.insights_summary,
            "cs_insights": self.cs_summary,
            "engagement": self.glean_summary,
            "glean": self.glean_summary,
            "planhat": self.planhat_summary,
            "computed_at": self.computed_at,
        }


# ---------------------------------------------------------------------------
# Weight configuration — tune these to change how heavily each signal
# contributes to the overall escalation risk score.
# ---------------------------------------------------------------------------
WEIGHTS = {
    "sf_open_case_volume":    0.10,
    "sf_p1_p2_ratio":         0.10,
    "sf_escalation_history":  0.08,
    "sf_case_aging":          0.05,
    "sf_repeat_issues":       0.04,
    "insights_critical_alerts": 0.07,
    "insights_unhealthy_clusters": 0.06,
    "insights_ncc_failures":  0.03,
    "insights_contract":      0.04,
    "insights_pulse":         0.03,
    "insights_eol":           0.04,
    "insights_advisory":      0.06,
    "cs_health_score":        0.05,
    "cs_engagement":          0.03,
    "cs_license_adoption":    0.05,
    "cs_renewal_proximity":   0.05,
    "cs_renewal_risk":        0.04,
    "cs_sentiment":           0.03,
    "custom_manual":          0.05,
    "glean_internal_engagement": 0.04,
    "glean_escalation_signals":  0.03,
}


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


class RiskScorer:
    """Stateless scorer — call ``score_account`` with pre-fetched data."""

    def score_account(
        self,
        account_id: str,
        account_name: str,
        sf_data: dict[str, Any],
        insights_data: dict[str, Any],
        cs_data: dict[str, Any],
        custom_risks: list[dict[str, Any]] | None = None,
        glean_data: dict[str, Any] | None = None,
        planhat_data: dict[str, Any] | None = None,
    ) -> AccountRiskProfile:
        factors: list[RiskFactor] = []
        recommendations: list[str] = []

        # ── Salesforce signals ───────────────────────────────────────────
        factors.extend(self._score_salesforce(sf_data, recommendations))

        # ── Nutanix Insights signals ─────────────────────────────────────
        factors.extend(self._score_insights(insights_data, recommendations))

        # ── CS Insights signals ──────────────────────────────────────────
        factors.extend(self._score_cs(cs_data, recommendations))

        # ── Glean internal engagement signals ────────────────────────────
        factors.extend(self._score_glean(glean_data or {}, recommendations))

        # ── Planhat signals ──────────────────────────────────────────────
        # v1 surfaces Planhat data alongside the existing connectors but
        # leaves WEIGHTS untouched (data appears in the UI but does not
        # contribute to the overall score yet). Hook is in place so a
        # follow-up can introduce weights without touching the call site.
        factors.extend(self._score_planhat(planhat_data or {}, recommendations))

        # ── CXM/TAM custom risk flags ────────────────────────────────────
        factors.extend(self._score_custom_factors(custom_risks or [], recommendations))

        overall = _clamp(sum(f.weighted_score for f in factors))
        risk_level = self._level(overall)

        if overall >= settings.risk_threshold_high:
            recommendations.insert(
                0, "URGENT: Schedule executive review — score exceeds critical threshold."
            )

        profile = AccountRiskProfile(
            account_id=account_id,
            account_name=account_name,
            overall_score=overall,
            risk_level=risk_level,
            factors=factors,
            recommendations=recommendations,
            sf_summary=sf_data,
            insights_summary=insights_data,
            cs_summary=cs_data,
            glean_summary=glean_data or {},
            planhat_summary=planhat_data or {},
            computed_at=datetime.now(timezone.utc).isoformat(),
        )
        return profile

    # ── Planhat sub-scorer ──────────────────────────────────────────────
    # Stub returns no factors in v1 (no weight changes per the
    # "leave thresholds and weights as-is" decision). The signature
    # matches the other _score_* methods so a follow-up can drop in
    # weighted factors without touching score_account.
    def _score_planhat(
        self, data: dict[str, Any], recs: list[str],  # noqa: ARG002
    ) -> list[RiskFactor]:
        return []

    # ── Salesforce sub-scorers ───────────────────────────────────────────

    def _score_salesforce(
        self, data: dict[str, Any], recs: list[str]
    ) -> list[RiskFactor]:
        factors: list[RiskFactor] = []
        if not data:
            return factors

        open_count = data.get("open_cases", 0)
        total = data.get("total_cases", 0)
        escalated = data.get("escalated_cases", 0)
        p1 = data.get("p1_cases", 0)
        p2 = data.get("p2_cases", 0)

        # Open case volume (>20 open = score 100)
        vol_score = _clamp(open_count * 5)
        factors.append(RiskFactor(
            "salesforce", "case_volume",
            f"{open_count} open cases",
            WEIGHTS["sf_open_case_volume"], vol_score,
        ))

        # P1/P2 severity ratio (among open cases only)
        if open_count > 0:
            sev_pct = ((p1 + p2) / open_count) * 100
        else:
            sev_pct = 0
        sev_score = _clamp(sev_pct * 1.5)
        factors.append(RiskFactor(
            "salesforce", "severity_ratio",
            f"{p1} P1 + {p2} P2 out of {open_count} open cases ({sev_pct:.0f}%)",
            WEIGHTS["sf_p1_p2_ratio"], sev_score,
        ))
        if p1 >= 3:
            recs.append(f"Account has {p1} open P1 cases — ensure dedicated engineering attention.")

        # Active escalations
        esc_score = _clamp(escalated * 20)
        factors.append(RiskFactor(
            "salesforce", "escalation_history",
            f"{escalated} open escalated cases",
            WEIGHTS["sf_escalation_history"], esc_score,
        ))
        if escalated >= 2:
            recs.append("Multiple open escalations — review root cause patterns.")

        # Case aging (% of open cases older than 14 days)
        cases = data.get("cases", [])
        aged = self._count_aged_cases(cases, age_days=14)
        age_pct = (aged / open_count * 100) if open_count else 0
        age_score = _clamp(age_pct)
        factors.append(RiskFactor(
            "salesforce", "case_aging",
            f"{aged}/{open_count} open cases older than 14 days",
            WEIGHTS["sf_case_aging"], age_score,
        ))
        if aged > 3:
            recs.append(f"{aged} cases are aging beyond 14 days — prioritise resolution.")

        # Repeat issues (same Subject appearing >2 times)
        repeats = self._detect_repeats(cases)
        rep_score = _clamp(len(repeats) * 25)
        factors.append(RiskFactor(
            "salesforce", "repeat_issues",
            f"{len(repeats)} repeat issue patterns detected",
            WEIGHTS["sf_repeat_issues"], rep_score,
        ))
        if repeats:
            recs.append(f"Repeat issues found: {', '.join(repeats[:3])}.")

        return factors

    @staticmethod
    def _count_aged_cases(cases: list[dict], age_days: int) -> int:
        now = datetime.now(timezone.utc)
        count = 0
        for c in cases:
            if c.get("IsClosed", False):
                continue
            created = c.get("CreatedDate")
            if not created:
                continue
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if (now - dt).days >= age_days:
                    count += 1
            except (ValueError, TypeError):
                pass
        return count

    @staticmethod
    def _detect_repeats(cases: list[dict], threshold: int = 2) -> list[str]:
        subjects: dict[str, int] = {}
        for c in cases:
            subj = (c.get("Subject") or "").strip().lower()
            if subj:
                subjects[subj] = subjects.get(subj, 0) + 1
        return [s for s, n in subjects.items() if n > threshold]

    # ── Nutanix Insights sub-scorers ─────────────────────────────────────

    def _score_insights(
        self, data: dict[str, Any], recs: list[str]
    ) -> list[RiskFactor]:
        factors: list[RiskFactor] = []
        if not data:
            return factors

        crit_alerts = data.get("total_critical_alerts", 0)
        unhealthy = data.get("unhealthy_clusters", 0)
        total_clusters = data.get("total_clusters", 0)
        ncc_fail = data.get("total_ncc_failures", 0)

        # Critical alerts
        crit_score = _clamp(crit_alerts * 15)
        factors.append(RiskFactor(
            "insights", "critical_alerts",
            f"{crit_alerts} critical alerts across {total_clusters} clusters",
            WEIGHTS["insights_critical_alerts"], crit_score,
        ))
        if crit_alerts > 0:
            recs.append(f"{crit_alerts} critical Insights alert(s) — immediate triage required.")

        # Unhealthy clusters
        if total_clusters > 0:
            uh_pct = (unhealthy / total_clusters) * 100
        else:
            uh_pct = 0
        uh_score = _clamp(uh_pct * 1.5)
        factors.append(RiskFactor(
            "insights", "unhealthy_clusters",
            f"{unhealthy}/{total_clusters} clusters reporting unhealthy",
            WEIGHTS["insights_unhealthy_clusters"], uh_score,
        ))

        # NCC failures
        ncc_score = _clamp(ncc_fail * 10)
        factors.append(RiskFactor(
            "insights", "ncc_failures",
            f"{ncc_fail} NCC check failures",
            WEIGHTS["insights_ncc_failures"], ncc_score,
        ))
        if ncc_fail > 5:
            recs.append(f"{ncc_fail} NCC failures — run a full health check.")

        # Contract status
        contract = data.get("contract_status", "unknown")
        contract_lower = str(contract).lower()
        if contract_lower in ("true", "valid", "active"):
            contract_score = 0.0
        elif contract_lower in ("false", "expired"):
            contract_score = 100.0
            recs.append("Contract has expired — renewal urgently needed to maintain support.")
        else:
            contract_score = 30.0
        factors.append(RiskFactor(
            "insights", "contract_status",
            f"Contract status: {contract}",
            WEIGHTS["insights_contract"], contract_score,
        ))

        # Pulse connectivity — granular connected/disconnected counts
        pulse = data.get("pulse_enabled")
        pulse_conn = data.get("pulse_connected", 0)
        pulse_disc = data.get("pulse_disconnected", 0)
        pulse_total = pulse_conn + pulse_disc
        if pulse_conn or pulse_disc:
            if pulse_disc > 0:
                pulse_score = _clamp(pulse_disc / max(pulse_total, 1) * 100)
                recs.append(
                    f"{pulse_disc} of {pulse_total} cluster(s) have Pulse disabled — "
                    "enable Pulse for proactive monitoring and support."
                )
            else:
                pulse_score = 0.0
            pulse_desc = f"Pulse: {pulse_conn} active, {pulse_disc} disabled (of {pulse_total})"
        elif pulse is False:
            pulse_score = 80.0
            pulse_desc = "Pulse telemetry is disabled"
            recs.append("Pulse telemetry is disabled — Nutanix cannot proactively monitor clusters.")
        elif pulse is True:
            pulse_score = 0.0
            pulse_desc = "Pulse enabled (account-level)"
        else:
            pulse_score = 20.0
            pulse_desc = "Pulse status unknown"
        factors.append(RiskFactor(
            "insights", "pulse_connectivity",
            pulse_desc,
            WEIGHTS["insights_pulse"], pulse_score,
        ))

        # EOL exposure — with approaching-EOL breakdown
        eol_count = data.get("eol_exposure_count", 0)
        eol_versions = data.get("eol_versions_in_use", [])
        approaching_eol_count = data.get("approaching_eol_count", 0)
        approaching_eol_versions = data.get("approaching_eol_versions", [])
        clusters = data.get("clusters", [])
        combined_eol = eol_count + approaching_eol_count
        eol_score = _clamp(eol_count * 30 + approaching_eol_count * 10)
        eol_parts = []
        if eol_count:
            eol_parts.append(f"{eol_count} EOL")
        if approaching_eol_count:
            eol_parts.append(f"{approaching_eol_count} approaching EOL")
        eol_desc = f"{combined_eol} nodes at risk — " + ", ".join(eol_parts) if eol_parts else "No EOL exposure"
        factors.append(RiskFactor(
            "insights", "eol_exposure",
            eol_desc,
            WEIGHTS["insights_eol"], eol_score,
        ))
        if eol_count > 0:
            eol_detail_parts = []
            for ver in eol_versions[:5]:
                matching = [c for c in clusters if c.get("aos_version") == ver]
                node_total = sum(c.get("node_count", 0) for c in matching)
                cluster_ids = [c.get("cluster_id", "") for c in matching]
                eol_detail_parts.append(
                    f"AOS {ver} ({node_total} node(s) in {', '.join(cluster_ids[:3])})"
                )
            recs.append(
                f"{eol_count} node(s) on EOL software — upgrade required: "
                + "; ".join(eol_detail_parts)
                + "."
            )
        if approaching_eol_count > 0:
            app_detail_parts = []
            for ver in approaching_eol_versions[:5]:
                matching = [c for c in clusters if c.get("aos_version") == ver]
                node_total = sum(c.get("node_count", 0) for c in matching)
                cluster_ids = [c.get("cluster_id", "") for c in matching]
                app_detail_parts.append(
                    f"AOS {ver} ({node_total} node(s) in {', '.join(cluster_ids[:3])})"
                )
            recs.append(
                f"{approaching_eol_count} node(s) approaching EOL within 180 days — plan upgrades: "
                + "; ".join(app_detail_parts)
                + "."
            )

        # Advisory compliance — with specific cluster/asset names
        adv_stats = data.get("advisory_stats", {})
        adv_total = adv_stats.get("total", 0)
        adv_crit = adv_stats.get("critical", 0)
        adv_high = adv_stats.get("high", 0)
        advisories = data.get("applicable_advisories", [])
        if adv_total > 0:
            adv_score = _clamp(adv_crit * 30 + adv_high * 15 + (adv_total - adv_crit - adv_high) * 5)
            parts = []
            if adv_crit:
                parts.append(f"{adv_crit} critical")
            if adv_high:
                parts.append(f"{adv_high} high")
            remaining = adv_total - adv_crit - adv_high
            if remaining:
                parts.append(f"{remaining} other")
            adv_desc = f"{adv_total} applicable advisories ({', '.join(parts)})"
        else:
            adv_score = 0.0
            adv_desc = "No applicable advisories"
        factors.append(RiskFactor(
            "insights", "advisory_compliance",
            adv_desc,
            WEIGHTS["insights_advisory"], adv_score,
        ))

        for adv in advisories:
            sev = adv.get("severity", "").lower()
            if sev not in ("critical", "high"):
                continue
            title = adv.get("title") or adv.get("number", "Advisory")
            affected_cls = adv.get("affected_clusters", [])
            reasons = adv.get("match_reasons", [])
            if affected_cls:
                cluster_names = [
                    f"{ac['cluster_id']} (AOS {ac['aos_version']}, {ac['node_count']} nodes)"
                    for ac in affected_cls[:3]
                ]
                reason_str = f" — {reasons[0]}" if reasons else ""
                recs.append(
                    f"{sev.capitalize()} advisory \"{title}\"{reason_str}: "
                    f"affects {', '.join(cluster_names)}"
                    + (f" and {len(affected_cls) - 3} more" if len(affected_cls) > 3 else "")
                    + ". Review and apply patches."
                )
            else:
                recs.append(
                    f"{sev.capitalize()} advisory \"{title}\" affects this infrastructure — "
                    "review and apply recommended patches."
                )

        return factors

    # ── CS Insights sub-scorers ──────────────────────────────────────────

    def _score_cs(
        self, data: dict[str, Any], recs: list[str]
    ) -> list[RiskFactor]:
        factors: list[RiskFactor] = []
        if not data:
            return factors

        health_src = data.get("health_source", "estimated")
        src_tag = f" [{health_src}]" if health_src != "live" else ""

        # Health score (inverted — low health = high risk)
        hs = data.get("health_score", 50)
        hs_risk = _clamp(100 - hs)
        factors.append(RiskFactor(
            "csinsights", "health_score",
            f"CS health score {hs}/100{src_tag}",
            WEIGHTS["cs_health_score"], hs_risk,
        ))
        if hs < 40:
            recs.append("CS health score is critically low — engage Customer Success team.")

        # Engagement (inverted)
        eng = data.get("engagement_score", 50)
        eng_src = data.get("engagement_source", "estimated")
        eng_tag = f" [{eng_src}]" if eng_src != "live" else ""
        eng_risk = _clamp(100 - eng)
        factors.append(RiskFactor(
            "csinsights", "engagement",
            f"Engagement score {eng}/100{eng_tag}",
            WEIGHTS["cs_engagement"], eng_risk,
        ))

        # License adoption (low adoption = high risk)
        adoption = data.get("adoption_score", 0)
        if adoption > 0:
            adopt_risk = _clamp(100 - adoption)
            factors.append(RiskFactor(
                "csinsights", "license_adoption",
                f"License adoption {adoption:.0f}%",
                WEIGHTS["cs_license_adoption"], adopt_risk,
            ))
            if adoption < 30:
                recs.append(
                    f"License adoption is only {adoption:.0f}% — "
                    "schedule product enablement session."
                )
        else:
            factors.append(RiskFactor(
                "csinsights", "license_adoption",
                "License adoption data unavailable",
                WEIGHTS["cs_license_adoption"], 30.0,
            ))

        # Renewal proximity (closer = higher risk)
        days_to_renewal = data.get("days_to_renewal")
        if days_to_renewal is not None and days_to_renewal < 365:
            ren_score = _clamp((365 - days_to_renewal) / 3.65)
            factors.append(RiskFactor(
                "csinsights", "renewal_proximity",
                f"Renewal in {days_to_renewal} days",
                WEIGHTS["cs_renewal_proximity"], ren_score,
            ))
            if days_to_renewal < 60:
                recs.append(
                    f"Renewal in {days_to_renewal} days — "
                    "escalation could jeopardise renewal."
                )
        else:
            desc = "Renewal >365 days away" if days_to_renewal else "Renewal date unknown"
            factors.append(RiskFactor(
                "csinsights", "renewal_proximity",
                desc,
                WEIGHTS["cs_renewal_proximity"], 0,
            ))

        # Renewal risk (from Salesforce Opportunity data)
        renewal_risk_score = data.get("renewal_risk_score")
        renewal_risk_label = data.get("renewal_risk_label", "unknown")
        if renewal_risk_score is not None:
            rr_risk = _clamp(renewal_risk_score)
            factors.append(RiskFactor(
                "csinsights", "renewal_risk",
                f"Renewal risk {renewal_risk_score:.0f}% — {renewal_risk_label}",
                WEIGHTS["cs_renewal_risk"], rr_risk,
            ))
            if renewal_risk_score >= 70:
                recs.append(
                    f"High renewal risk ({renewal_risk_score:.0f}%): {renewal_risk_label}. "
                    "Proactive account review recommended."
                )
        else:
            factors.append(RiskFactor(
                "csinsights", "renewal_risk",
                "Renewal risk data unavailable",
                WEIGHTS["cs_renewal_risk"], 20.0,
            ))

        # Sentiment
        nps = data.get("nps_score")
        label = data.get("sentiment_label", "unknown")
        sent_src = data.get("sentiment_source", "estimated")
        if nps is not None:
            sent_risk = _clamp(max(0, 50 - nps) * 2)
            desc = f"NPS: {nps}, Sentiment: {label}"
        elif label in ("negative", "detractor"):
            sent_risk = 80.0
            desc = f"Sentiment: {label} [{sent_src}]"
        elif label in ("neutral", "passive"):
            sent_risk = 40.0
            desc = f"Sentiment: {label} [{sent_src}]"
        elif label == "positive":
            sent_risk = 10.0
            desc = f"Sentiment: {label} [{sent_src}]"
        else:
            sent_risk = 20.0
            desc = "Sentiment: unknown"
        factors.append(RiskFactor(
            "csinsights", "sentiment",
            desc,
            WEIGHTS["cs_sentiment"], sent_risk,
        ))
        if label in ("negative", "detractor"):
            recs.append("Negative customer sentiment detected — proactive outreach recommended.")

        return factors

    # ── Internal engagement sub-scorer ───────────────────────────────

    def _score_glean(
        self, data: dict[str, Any], recs: list[str]
    ) -> list[RiskFactor]:
        factors: list[RiskFactor] = []
        if not data:
            return factors

        total = data.get("total_mentions", 0)
        recent = data.get("recent_mentions_30d", 0)
        escalation = data.get("escalation_mentions", 0)
        comments = data.get("knowledge_articles", 0)
        trend = data.get("mention_trend", "none")

        if total == 0:
            eng_score = 80.0
            eng_desc = "No internal activity (tasks/events/comments) — organizational blind spot"
            recs.append(
                "Zero internal activity found for this account — "
                "the team may lack awareness. Initiate internal engagement."
            )
        elif total < 5:
            eng_score = 60.0
            eng_desc = f"Only {total} internal touchpoints — low engagement"
            recs.append(
                f"Only {total} internal touchpoint(s) — increase "
                "internal engagement and documentation."
            )
        elif total < 20:
            eng_score = 30.0
            eng_desc = f"{total} internal touchpoints — moderate engagement"
        else:
            eng_score = max(0, 20.0 - total * 0.2)
            eng_desc = f"{total} internal touchpoints — good engagement"

        if trend == "declining" and total > 5:
            eng_score = min(100, eng_score + 15)
            eng_desc += " (declining trend)"

        if recent == 0 and total > 5:
            eng_score = min(100, eng_score + 10)
            recs.append("No internal activity in the last 30 days — engagement may have stalled.")

        factors.append(RiskFactor(
            "glean", "internal_engagement",
            eng_desc,
            WEIGHTS["glean_internal_engagement"], _clamp(eng_score),
        ))

        if escalation > 0:
            esc_score = _clamp(min(100, escalation * 25))
            factors.append(RiskFactor(
                "glean", "escalation_signals",
                f"{escalation} escalated/P1-P2 case(s) in 180 days",
                WEIGHTS["glean_escalation_signals"], esc_score,
            ))
            recs.append(
                f"{escalation} escalated or P1/P2 case(s) detected — "
                "review for emerging risk patterns."
            )
        else:
            factors.append(RiskFactor(
                "glean", "escalation_signals",
                "No escalation signals detected",
                WEIGHTS["glean_escalation_signals"], 0.0,
            ))

        if comments == 0 and total > 5:
            recs.append(
                "No internal case comments found — "
                "create internal documentation to support the team."
            )

        return factors

    # ── CXM/TAM custom risk sub-scorer ──────────────────────────────────

    def _score_custom_factors(
        self, custom_risks: list[dict[str, Any]], recs: list[str]
    ) -> list[RiskFactor]:
        factors: list[RiskFactor] = []
        active = [r for r in custom_risks if r.get("status") == "active"]
        if not active:
            factors.append(RiskFactor(
                "cxm", "custom_manual",
                "No active CXM/TAM risk flags",
                WEIGHTS["custom_manual"], 0.0,
            ))
            return factors

        severity_scores = {"critical": 30, "high": 20, "medium": 10, "low": 5}
        raw = min(100.0, sum(severity_scores.get(r.get("severity", "medium"), 10) for r in active))
        crit_count = sum(1 for r in active if r.get("severity") == "critical")
        high_count = sum(1 for r in active if r.get("severity") == "high")

        parts = [f"{len(active)} active CXM risk flag(s)"]
        if crit_count:
            parts.append(f"{crit_count} critical")
        if high_count:
            parts.append(f"{high_count} high")

        factors.append(RiskFactor(
            "cxm", "custom_manual",
            " — ".join(parts),
            WEIGHTS["custom_manual"], raw,
        ))

        if crit_count:
            titles = [r.get("title", r.get("category", "")) for r in active if r.get("severity") == "critical"]
            recs.append(f"CXM flagged {crit_count} critical risk(s): {', '.join(titles[:3])}.")
        elif high_count:
            recs.append(f"CXM flagged {high_count} high-severity risk(s) — review during next account review.")

        return factors

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _level(score: float) -> str:
        if score >= settings.risk_threshold_high:
            return "critical"
        if score >= settings.risk_threshold_medium:
            return "high"
        if score >= 25:
            return "medium"
        return "low"
