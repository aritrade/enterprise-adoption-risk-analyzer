"""
Email draft generator — transforms risk analysis data into professional,
actionable emails that TAMs/CXMs can send to customer stakeholders.

Generates both plain-text and HTML versions of the email.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.config import settings


def generate_email_draft(
    risk_data: dict[str, Any],
    contacts: list[dict[str, Any]],
    sender_name: str = "",
    include_license: bool = False,
    license_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build a complete email draft from risk analysis results.

    Returns a dict with: subject, to, cc, body_text, body_html,
    and structured problem/recommendation data for the UI.
    """
    account_name = risk_data.get("account_name", "Unknown Account")
    risk_level = risk_data.get("risk_level", "unknown")
    score = risk_data.get("overall_score", 0)
    sf = risk_data.get("salesforce", {})
    ins = risk_data.get("insights", {})
    cs = risk_data.get("cs_insights", {})
    factors = risk_data.get("factors", [])
    recommendations = risk_data.get("recommendations", [])

    flagged_problems = _extract_flagged_problems(factors, sf, ins, cs)
    action_items = _build_action_items(recommendations, flagged_problems)

    primary_contacts = [c for c in contacts if c.get("is_primary")]
    other_contacts = [c for c in contacts if not c.get("is_primary")]

    to_emails = [c["Email"] for c in primary_contacts[:3] if c.get("Email")]
    cc_emails = [c["Email"] for c in other_contacts[:5] if c.get("Email")]

    subject = _build_subject(account_name, risk_level, score)
    body_text = _build_body_text(
        account_name, score, risk_level,
        flagged_problems, action_items, sf, ins,
        include_license, license_data,
        sender_name or settings.email_sender_name,
    )
    body_html = _build_body_html(
        account_name, score, risk_level,
        flagged_problems, action_items, sf, ins,
        include_license, license_data,
        sender_name or settings.email_sender_name,
    )

    return {
        "subject": subject,
        "to": to_emails,
        "cc": cc_emails,
        "body_text": body_text,
        "body_html": body_html,
        "flagged_problems": flagged_problems,
        "action_items": action_items,
        "all_contacts": [
            {
                "name": c.get("Name", ""),
                "email": c.get("Email", ""),
                "title": c.get("Title", ""),
                "is_primary": c.get("is_primary", False),
            }
            for c in contacts
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _extract_flagged_problems(
    factors: list[dict], sf: dict, ins: dict, cs: dict
) -> list[dict[str, Any]]:
    """Identify the most significant problems from risk factor data."""
    problems: list[dict[str, Any]] = []

    high_factors = sorted(factors, key=lambda f: f.get("weighted_score", 0), reverse=True)
    for f in high_factors:
        if f.get("weighted_score", 0) < 2.0:
            continue
        severity = "critical" if f["weighted_score"] >= 8 else "high" if f["weighted_score"] >= 5 else "medium"
        problems.append({
            "source": f["source"],
            "category": f["category"],
            "description": f["description"],
            "severity": severity,
            "score": round(f["weighted_score"], 1),
        })

    p1 = sf.get("p1_cases", 0)
    if p1 > 0 and not any(p["category"] == "active_p1_cases" for p in problems):
        problems.append({
            "source": "salesforce",
            "category": "active_p1_cases",
            "description": f"{p1} active P1 (Severity 1) case(s) requiring immediate attention",
            "severity": "critical",
            "score": p1 * 5,
        })

    eol_count = ins.get("eol_exposure_count", 0)
    if eol_count > 0 and not any(p["category"] == "eol_software" for p in problems):
        eol_versions = ins.get("eol_versions_in_use", [])
        problems.append({
            "source": "insights",
            "category": "eol_software",
            "description": f"{eol_count} cluster(s) running end-of-life software ({', '.join(eol_versions)})",
            "severity": "high",
            "score": eol_count * 4,
        })

    if ins.get("pulse_enabled") is False and not any(p["category"] == "pulse_disabled" for p in problems):
        problems.append({
            "source": "insights",
            "category": "pulse_disabled",
            "description": "Pulse telemetry is disabled — proactive monitoring is not active",
            "severity": "medium",
            "score": 4,
        })

    problems.sort(key=lambda p: p["score"], reverse=True)
    return problems


def _build_action_items(
    recommendations: list[str], problems: list[dict]
) -> list[dict[str, str]]:
    """Build prioritised action items from recommendations and problems."""
    items: list[dict[str, str]] = []
    seen: set[str] = set()

    for rec in recommendations:
        key = rec[:60].lower()
        if key in seen:
            continue
        seen.add(key)
        if any(w in rec.lower() for w in ("urgent", "critical", "immediate")):
            priority = "critical"
        elif any(w in rec.lower() for w in ("recurring", "escalation", "expired", "eol")):
            priority = "high"
        else:
            priority = "medium"
        items.append({"priority": priority, "action": rec})

    return items


def _build_subject(account_name: str, risk_level: str, score: float) -> str:
    level_labels = {
        "critical": "CRITICAL",
        "high": "HIGH",
        "medium": "MEDIUM",
        "low": "LOW",
    }
    label = level_labels.get(risk_level, "")
    if risk_level in ("critical", "high"):
        return f"[{label} RISK] {account_name} — Infrastructure Health Review & Recommended Actions"
    return f"[{label} RISK] {account_name} — Proactive Health Review & Recommendations"


def _build_body_text(
    account_name: str, score: float, risk_level: str,
    problems: list[dict], actions: list[dict],
    sf: dict, ins: dict,
    include_license: bool, license_data: dict | None,
    sender_name: str,
) -> str:
    lines: list[str] = []
    lines.append(f"Dear {account_name} Team,")
    lines.append("")

    if risk_level in ("critical", "high"):
        lines.append(
            "As part of our ongoing commitment to your infrastructure success, "
            "our proactive monitoring and analysis has identified several areas "
            "that require attention in your Nutanix environment. We'd like to "
            "bring these to your notice and work with you on resolving them promptly."
        )
    else:
        lines.append(
            "As part of our regular health review of your Nutanix environment, "
            "we've identified a few items that we'd like to bring to your attention "
            "along with our recommendations for optimisation."
        )
    lines.append("")

    lines.append(f"ENVIRONMENT SUMMARY")
    lines.append(f"{'─' * 40}")
    lines.append(f"  Overall Health Score: {score}/100 ({risk_level.upper()} risk)")
    if sf:
        lines.append(f"  Open Support Cases:  {sf.get('open_cases', 0)} (P1: {sf.get('p1_cases', 0)}, P2: {sf.get('p2_cases', 0)})")
        if sf.get("escalated_cases", 0) > 0:
            lines.append(f"  Escalated Cases:     {sf['escalated_cases']}")
    if ins:
        lines.append(f"  Total Clusters:      {ins.get('total_clusters', 0)}")
        if ins.get("eol_exposure_count", 0) > 0:
            lines.append(f"  EOL Exposure:        {ins['eol_exposure_count']} cluster(s)")
    lines.append("")

    if problems:
        lines.append("FLAGGED ITEMS REQUIRING ATTENTION")
        lines.append(f"{'─' * 40}")
        for i, p in enumerate(problems, 1):
            sev = p["severity"].upper()
            lines.append(f"  {i}. [{sev}] {p['description']}")
        lines.append("")

    if actions:
        lines.append("RECOMMENDED ACTIONS")
        lines.append(f"{'─' * 40}")
        for i, a in enumerate(actions, 1):
            lines.append(f"  {i}. {a['action']}")
        lines.append("")

    if include_license and license_data:
        products = license_data.get("products", [])
        overall_pct = license_data.get("overall_adoption_pct", 0)
        if products:
            lines.append("LICENSE ADOPTION SUMMARY")
            lines.append(f"{'─' * 40}")
            lines.append(f"  Overall Adoption: {overall_pct}%")
            low_adoption = [p for p in products if p.get("adoption_pct", 100) < 50 and p.get("capacity", 0) > 0]
            for p in low_adoption:
                lines.append(f"  • {p['product']}: {p['adoption_pct']}% adopted ({p['activated']}/{p['capacity']} licenses)")
            lines.append("")

    lines.append("NEXT STEPS")
    lines.append(f"{'─' * 40}")
    lines.append("  We'd like to schedule a brief review session to discuss these findings")
    lines.append("  and align on a remediation plan. Please let us know your availability")
    lines.append("  for a 30-minute call in the coming week.")
    lines.append("")
    lines.append("  If any of the above items are urgent or if you have questions,")
    lines.append("  please don't hesitate to reach out directly.")
    lines.append("")
    lines.append(settings.email_signature)

    return "\n".join(lines)


def _build_body_html(
    account_name: str, score: float, risk_level: str,
    problems: list[dict], actions: list[dict],
    sf: dict, ins: dict,
    include_license: bool, license_data: dict | None,
    sender_name: str,
) -> str:
    risk_colors = {
        "critical": "#ff4757",
        "high": "#ff8c42",
        "medium": "#ffc83d",
        "low": "#2ed573",
    }
    color = risk_colors.get(risk_level, "#8fa3b8")

    if risk_level in ("critical", "high"):
        intro = (
            "As part of our ongoing commitment to your infrastructure success, "
            "our proactive monitoring and analysis has identified several areas "
            "that require attention in your Nutanix environment. We'd like to "
            "bring these to your notice and work with you on resolving them promptly."
        )
    else:
        intro = (
            "As part of our regular health review of your Nutanix environment, "
            "we've identified a few items that we'd like to bring to your attention "
            "along with our recommendations for optimisation."
        )

    summary_rows = f"""
        <tr><td style="padding:6px 12px;color:#8fa3b8">Overall Health Score</td>
            <td style="padding:6px 12px;font-weight:600;color:{color}">{score}/100 ({risk_level.upper()})</td></tr>"""
    if sf:
        p1_tag = f' <span style="background:rgba(255,71,87,0.2);color:#ff4757;padding:1px 6px;border-radius:10px;font-size:0.75em">P1: {sf.get("p1_cases",0)}</span>' if sf.get("p1_cases", 0) else ""
        summary_rows += f"""
        <tr><td style="padding:6px 12px;color:#8fa3b8">Open Cases</td>
            <td style="padding:6px 12px">{sf.get('open_cases',0)}{p1_tag}</td></tr>"""
        if sf.get("escalated_cases", 0) > 0:
            summary_rows += f"""
        <tr><td style="padding:6px 12px;color:#8fa3b8">Escalated Cases</td>
            <td style="padding:6px 12px;color:#ff8c42">{sf['escalated_cases']}</td></tr>"""
    if ins:
        summary_rows += f"""
        <tr><td style="padding:6px 12px;color:#8fa3b8">Clusters</td>
            <td style="padding:6px 12px">{ins.get('total_clusters',0)}</td></tr>"""
        if ins.get("eol_exposure_count", 0) > 0:
            summary_rows += f"""
        <tr><td style="padding:6px 12px;color:#8fa3b8">EOL Exposure</td>
            <td style="padding:6px 12px;color:#ff8c42">{ins['eol_exposure_count']} cluster(s)</td></tr>"""

    sev_colors = {"critical": "#ff4757", "high": "#ff8c42", "medium": "#ffc83d"}
    problems_html = ""
    if problems:
        problem_rows = ""
        for p in problems:
            sc = sev_colors.get(p["severity"], "#8fa3b8")
            badge = f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;font-size:0.7em;font-weight:600;background:rgba({_hex_to_rgba(sc)},0.15);color:{sc}">{p["severity"].upper()}</span>'
            problem_rows += f'<tr><td style="padding:8px 12px;vertical-align:top">{badge}</td><td style="padding:8px 12px">{_esc(p["description"])}</td></tr>'
        problems_html = f"""
        <table style="width:100%;border-collapse:collapse;margin-bottom:8px">
            <tr><th colspan="2" style="text-align:left;padding:10px 12px;background:#1a2736;color:#e8edf2;font-size:0.85em;border-radius:6px 6px 0 0">
                Flagged Items Requiring Attention</th></tr>
            {problem_rows}
        </table>"""

    actions_html = ""
    if actions:
        action_rows = "".join(
            f'<li style="padding:4px 0;color:#e8edf2">{_esc(a["action"])}</li>'
            for a in actions
        )
        actions_html = f"""
        <div style="background:#1e3044;border-radius:8px;padding:16px;margin-bottom:16px;border-left:3px solid #3b9eff">
            <div style="font-weight:600;color:#3b9eff;margin-bottom:8px;font-size:0.85em;text-transform:uppercase">Recommended Actions</div>
            <ol style="margin:0;padding-left:20px;font-size:0.9em">{action_rows}</ol>
        </div>"""

    license_html = ""
    if include_license and license_data:
        products = license_data.get("products", [])
        overall_pct = license_data.get("overall_adoption_pct", 0)
        low_adoption = [p for p in products if p.get("adoption_pct", 100) < 50 and p.get("capacity", 0) > 0]
        if low_adoption:
            lic_rows = "".join(
                f'<tr><td style="padding:4px 12px">{_esc(p["product"])}</td>'
                f'<td style="padding:4px 12px;color:{sev_colors.get("high" if p["adoption_pct"]<25 else "medium","#ffc83d")}">{p["adoption_pct"]}%</td>'
                f'<td style="padding:4px 12px;color:#8fa3b8">{p["activated"]}/{p["capacity"]}</td></tr>'
                for p in low_adoption
            )
            license_html = f"""
        <div style="margin-bottom:16px">
            <div style="font-weight:600;color:#e8edf2;margin-bottom:8px;font-size:0.85em">License Adoption (Overall: {overall_pct}%)</div>
            <table style="width:100%;border-collapse:collapse;font-size:0.85em">
                <tr style="color:#8fa3b8"><th style="text-align:left;padding:4px 12px">Product</th><th style="text-align:left;padding:4px 12px">Adoption</th><th style="text-align:left;padding:4px 12px">Active/Total</th></tr>
                {lic_rows}
            </table>
        </div>"""

    sig_html = settings.email_signature.replace("\n", "<br>")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f1923;color:#e8edf2;margin:0;padding:0">
<div style="max-width:680px;margin:0 auto;padding:32px 24px">

    <div style="border-left:4px solid {color};padding-left:16px;margin-bottom:24px">
        <h2 style="margin:0 0 4px;color:#e8edf2">Infrastructure Health Review</h2>
        <div style="color:#8fa3b8;font-size:0.9em">{_esc(account_name)}</div>
    </div>

    <p style="color:#c8d6e5;line-height:1.6;margin-bottom:20px">{_esc(f"Dear {account_name} Team,")}</p>
    <p style="color:#c8d6e5;line-height:1.6;margin-bottom:24px">{intro}</p>

    <div style="background:#1a2736;border-radius:8px;padding:4px 0;margin-bottom:20px">
        <table style="width:100%;border-collapse:collapse;font-size:0.9em">
            <tr><th colspan="2" style="text-align:left;padding:10px 12px;color:#8fa3b8;font-size:0.8em;text-transform:uppercase;letter-spacing:0.05em;border-bottom:1px solid #2a3f54">
                Environment Summary</th></tr>
            {summary_rows}
        </table>
    </div>

    {problems_html}
    {actions_html}
    {license_html}

    <div style="background:#1a2736;border-radius:8px;padding:16px;margin-bottom:24px">
        <div style="font-weight:600;color:#e8edf2;margin-bottom:8px">Next Steps</div>
        <p style="color:#c8d6e5;margin:0;line-height:1.6;font-size:0.9em">
            We'd like to schedule a brief review session to discuss these findings
            and align on a remediation plan. Please let us know your availability
            for a 30-minute call in the coming week.
        </p>
        <p style="color:#c8d6e5;margin:8px 0 0;line-height:1.6;font-size:0.9em">
            If any of the above items are urgent or if you have questions,
            please don't hesitate to reach out directly.
        </p>
    </div>

    <div style="border-top:1px solid #2a3f54;padding-top:16px;color:#8fa3b8;font-size:0.82em;line-height:1.6">
        {sig_html}
    </div>

</div>
</body></html>"""


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _hex_to_rgba(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"{r},{g},{b}"
