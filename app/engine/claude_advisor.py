"""
Optional Claude-powered advisory layer.

Turns a scored account risk profile into a concise, prioritized action plan
for a Customer Experience Manager. Calls the Anthropic Messages API when
``ANTHROPIC_API_KEY`` is configured; otherwise returns a deterministic
advisory built from the same signals — so the feature always works (and costs
nothing) without a key. The key only ever lives server-side.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

_SYSTEM_PROMPT = (
    "You are an enterprise customer-success advisor for an infrastructure vendor. "
    "Given one customer account's escalation-risk profile, produce a crisp, "
    "prioritized action plan a Customer Experience Manager can act on this week. "
    "Ground every point in the signals provided, call out the single biggest "
    "driver, and be honest about uncertainty. No filler, no invented data, no "
    "customer PII."
)


def _profile_brief(profile: dict[str, Any]) -> str:
    """Compact, token-bounded summary of the profile for the prompt."""
    name = profile.get("account_name", "Unknown")
    score = profile.get("overall_score")
    level = profile.get("risk_level")
    factors = sorted(
        profile.get("factors", []),
        key=lambda f: f.get("weighted_score", 0),
        reverse=True,
    )[:8]
    recs = profile.get("recommendations", [])[:8]

    lines = [
        f"Account: {name}",
        f"Overall escalation-risk score: {score}/100 ({level})",
        "",
        "Top weighted risk factors:",
    ]
    for f in factors:
        lines.append(
            f"- [{f.get('source')}] {f.get('description')} "
            f"(weight {round(f.get('weighted_score', 0), 1)})"
        )
    if recs:
        lines.append("")
        lines.append("System-generated recommendations:")
        for r in recs:
            lines.append(f"- {r}")
    return "\n".join(lines)


def _deterministic_advisory(profile: dict[str, Any]) -> dict[str, Any]:
    """Fallback advisory synthesized locally — no external calls, no cost."""
    score = profile.get("overall_score", 0)
    level = profile.get("risk_level", "unknown")
    factors = sorted(
        profile.get("factors", []),
        key=lambda f: f.get("weighted_score", 0),
        reverse=True,
    )
    recs = profile.get("recommendations", [])

    top = factors[0] if factors else None
    bullets: list[str] = []
    for r in recs[:6]:
        bullets.append(r)
    if not bullets:
        for f in factors[:6]:
            bullets.append(f"Address: {f.get('description')}")

    headline = (
        f"This account scores {score}/100 ({level} risk)."
        + (
            f" The single biggest driver is {top.get('description')} "
            f"(source: {top.get('source')})."
            if top else ""
        )
    )
    body = "\n".join(f"- {b}" for b in bullets) or "- No active risk drivers; maintain regular cadence."
    advisory = f"{headline}\n\nPrioritized actions for this week:\n{body}"
    return {"source": "deterministic", "model": None, "advisory": advisory}


async def generate_advisory(profile: dict[str, Any]) -> dict[str, Any]:
    """Return a Claude-generated advisory when a key is set, else deterministic."""
    key = settings.anthropic_api_key
    if not key:
        return _deterministic_advisory(profile)

    brief = _profile_brief(profile)
    user_msg = (
        "Here is the account's risk profile:\n\n"
        f"{brief}\n\n"
        "Write: (1) up to 6 prioritized action bullets, each one line, most "
        "urgent first; then (2) one sentence naming the single biggest risk to "
        "the relationship. Keep it under 160 words."
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _ANTHROPIC_URL,
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": settings.claude_model,
                    "max_tokens": 600,
                    "system": _SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": user_msg}],
                },
            )
        if resp.status_code != 200:
            logger.warning(
                "Claude advisory upstream %s: %s", resp.status_code, resp.text[:300]
            )
            out = _deterministic_advisory(profile)
            out["fallback_reason"] = f"upstream {resp.status_code}"
            return out
        data = resp.json()
        text = "\n".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        ).strip()
        return {
            "source": "claude",
            "model": settings.claude_model,
            "advisory": text or "(no response)",
        }
    except Exception as exc:  # network/timeout/etc — never break the UI
        logger.warning("Claude advisory failed: %s", exc)
        out = _deterministic_advisory(profile)
        out["fallback_reason"] = str(exc)[:120]
        return out
