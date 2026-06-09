"""
Planhat connector — pulls customer-success signals from the Planhat
tenant (``settings.planhat_base_url``, e.g. ``https://app.planhat.example/your-tenant``)
through the shared headless browser bridge.

No API keys, no stored credentials: the bridge replays the user's
authenticated browser session (seeded once via ``scripts/planhat_login.py``)
to hit Planhat's cookie-bound SPA endpoints.

Signals surfaced per account:
  - Health score + trend
  - NPS / CSAT (latest score + responded-at)
  - Lifecycle phase (onboarding / live / at-risk / churned)
  - Renewal date, ARR / MRR, churn-risk flag
  - Recent engagement (last meeting / email / touch)
  - Open tasks and conversations

Salesforce → Planhat mapping is by case-insensitive exact name match
and is cached in the ``planhat_account_map`` SQLite table the first
time a successful match lands.
"""
from __future__ import annotations

import asyncio
import logging
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Optional, TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    from app.connectors.browser_bridge import PlaywrightBridge
    from app.store.db import Database

logger = logging.getLogger(__name__)


def _planhat_source_label() -> str:
    """Stable per-tenant label used in snapshots (e.g. ``app.planhat.example``)."""
    parsed = urllib.parse.urlsplit(settings.planhat_base_url or "")
    return parsed.netloc or "planhat.com"


# How many recent activities/tasks/conversations to keep in the snapshot.
_ACTIVITY_LIMIT = 20
_TASK_LIMIT = 10
_CONVO_LIMIT = 10


# Endpoint candidates Planhat's SPA hits. The discovery dump
# (``.browser_state/planhat_api_calls.json``) confirms which ones exist
# on the live tenant; we try each in turn and silently skip 404s.
_SEARCH_CANDIDATES = (
    "/companies?search={q}&limit=5",
    "/companies?q={q}&limit=5",
    "/companies?name={q}&limit=5",
)

_COMPANY_DETAIL_CANDIDATES = (
    "/companies/{id}",
    "/companies/{id}/profile",
)

_HEALTH_CANDIDATES = (
    "/companies/{id}/health",
    "/companies/{id}/healthscore",
    "/companies/{id}/metrics/health",
)

_NPS_CANDIDATES = (
    "/companies/{id}/nps",
    "/companies/{id}/surveys/nps?limit=1",
)

_ACTIVITY_CANDIDATES = (
    "/companies/{id}/activities?limit={n}",
    "/companies/{id}/timeline?limit={n}",
)

_TASK_CANDIDATES = (
    "/companies/{id}/tasks?status=open&limit={n}",
    "/companies/{id}/tasks?completed=false&limit={n}",
)

_CONVO_CANDIDATES = (
    "/companies/{id}/conversations?status=open&limit={n}",
    "/companies/{id}/conversations?limit={n}",
)


class PlanhatConnector:
    """Reads customer-success data from the Planhat tenant via the browser bridge."""

    def __init__(
        self,
        bridge: "PlaywrightBridge",
        db: Optional["Database"] = None,
    ) -> None:
        from app.store.db import get_db
        self._bridge = bridge
        self._db = db or get_db()
        logger.info("Planhat connector initialized (browser bridge)")

    # ── Low-level helpers ────────────────────────────────────────────

    async def _get(self, path: str) -> Any:
        return await self._bridge.planhat_get(path)

    async def _try_endpoints(self, candidates: tuple[str, ...], **fmt: Any) -> Any:
        """Try each candidate path until one returns non-empty JSON."""
        for tmpl in candidates:
            path = tmpl.format(**fmt)
            try:
                data = await self._get(path)
                if data:
                    return data
            except PermissionError:
                raise
            except Exception as exc:
                logger.debug("Planhat %s skipped: %s", path, exc)
        return None

    # ── Company lookup + caching ─────────────────────────────────────

    async def _resolve_company_id(
        self, account_id: str, account_name: str,
    ) -> tuple[Optional[str], str]:
        """Return ``(planhat_id, match_method)`` for the given SF account.

        ``match_method`` is one of ``cached``, ``name_exact``,
        ``unmatched``. The mapping is persisted on first successful match.
        """
        cached = self._db.get_planhat_id(account_id)
        if cached:
            return cached, "cached"

        if not account_name:
            return None, "unmatched"

        # Hit any of the search-endpoint variants.
        search_data = await self._try_endpoints(_SEARCH_CANDIDATES, q=_quote(account_name))
        candidates = _extract_company_list(search_data)
        if not candidates:
            logger.info(
                "Planhat search returned no results for %r (sf=%s)",
                account_name, account_id,
            )
            return None, "unmatched"

        target = account_name.casefold().strip()
        for company in candidates:
            name = (company.get("name") or company.get("companyName") or "").casefold().strip()
            if name == target:
                ph_id = _company_id(company)
                if ph_id:
                    self._db.set_planhat_id(account_id, ph_id, "name_exact")
                    logger.info(
                        "Planhat mapped %s (%s) → %s", account_id, account_name, ph_id,
                    )
                    return ph_id, "name_exact"

        # No exact name match — refuse to auto-bind a fuzzy hit to avoid
        # cross-tenant pollution; surface the closest candidate in the log
        # for manual review.
        sample = candidates[0]
        logger.info(
            "Planhat search for %r yielded %d candidates; closest = %r (no auto-match)",
            account_name, len(candidates), sample.get("name") or sample.get("companyName"),
        )
        return None, "unmatched"

    # ── Public API ───────────────────────────────────────────────────

    async def get_account_planhat_summary(
        self, account_id: str, account_name: str = "",
    ) -> dict[str, Any]:
        """Pull a normalised Planhat snapshot for one Salesforce account.

        Raises on hard auth failure so ``BaseWorker.run_with_heal`` can
        decide to retry / fall back. Soft errors (a missing endpoint, a
        404 on a single sub-call) are absorbed: we return whichever
        signals we managed to collect.
        """
        ph_id, match_method = await self._resolve_company_id(account_id, account_name)
        if not ph_id:
            return self._empty_summary(
                account_id, account_name, match_method="unmatched",
            )

        # All sub-fetches run in parallel — the bridge serialises them
        # internally via the page lock, but launching them concurrently
        # is still cleaner than awaiting one at a time.
        results = await asyncio.gather(
            self._try_endpoints(_COMPANY_DETAIL_CANDIDATES, id=ph_id),
            self._try_endpoints(_HEALTH_CANDIDATES, id=ph_id),
            self._try_endpoints(_NPS_CANDIDATES, id=ph_id),
            self._try_endpoints(_ACTIVITY_CANDIDATES, id=ph_id, n=_ACTIVITY_LIMIT),
            self._try_endpoints(_TASK_CANDIDATES, id=ph_id, n=_TASK_LIMIT),
            self._try_endpoints(_CONVO_CANDIDATES, id=ph_id, n=_CONVO_LIMIT),
            return_exceptions=True,
        )
        company, health, nps, activities, tasks, conversations = (
            _unwrap(r) for r in results
        )

        # Bail loudly if every single sub-call failed with PermissionError —
        # that means the session is dead, not just that a route is missing.
        if all(isinstance(r, PermissionError) for r in results):
            raise PermissionError("Planhat session expired across all endpoints")

        return _build_summary(
            account_id=account_id,
            account_name=account_name,
            planhat_id=ph_id,
            match_method=match_method,
            company=company,
            health=health,
            nps=nps,
            activities=activities,
            tasks=tasks,
            conversations=conversations,
        )

    def _empty_summary(
        self, account_id: str, account_name: str, match_method: str = "unmatched",
    ) -> dict[str, Any]:
        return {
            "account_id": account_id,
            "account_name": account_name,
            "source": _planhat_source_label(),
            "planhat_company_id": None,
            "match_method": match_method,
            "health_score": None,
            "health_trend": "unknown",
            "lifecycle_phase": None,
            "nps_score": None,
            "nps_responded_at": None,
            "renewal_date": None,
            "days_to_renewal": None,
            "arr": None,
            "mrr": None,
            "churn_risk_flag": False,
            "last_meeting_at": None,
            "last_email_at": None,
            "last_touch_at": None,
            "open_tasks": [],
            "open_conversations": [],
            "custom_fields": {},
            "raw": {},
        }


# ──────────────────────────────────────────────────────────────────────
# Pure helpers
# ──────────────────────────────────────────────────────────────────────


def _quote(value: str) -> str:
    """URL-quote a search query while keeping it short."""
    import urllib.parse
    return urllib.parse.quote(value.strip()[:120])


def _unwrap(result: Any) -> Any:
    """Convert a ``gather(return_exceptions=True)`` slot into a clean value.

    ``PermissionError`` is propagated upward by the caller after inspecting
    the whole batch (we use it as the "session dead" signal). Any other
    exception or ``None`` collapses to ``None``.
    """
    if isinstance(result, PermissionError):
        return result
    if isinstance(result, Exception):
        return None
    return result


def _extract_company_list(payload: Any) -> list[dict[str, Any]]:
    """Normalise Planhat's various list shapes into a flat list of dicts."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [c for c in payload if isinstance(c, dict)]
    if isinstance(payload, dict):
        for key in ("companies", "results", "data", "items", "hits"):
            value = payload.get(key)
            if isinstance(value, list):
                return [c for c in value if isinstance(c, dict)]
    return []


def _company_id(company: dict[str, Any]) -> Optional[str]:
    for key in ("_id", "id", "companyId", "phId"):
        val = company.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, dict):
            inner = val.get("$oid") or val.get("oid")
            if isinstance(inner, str) and inner:
                return inner
    return None


def _first_number(*candidates: Any) -> Optional[float]:
    for value in candidates:
        if value is None:
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def _first_str(*candidates: Any) -> Optional[str]:
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _classify_trend(score: Optional[float], prev_score: Optional[float]) -> str:
    if score is None or prev_score is None:
        return "stable"
    diff = score - prev_score
    if diff >= 5:
        return "improving"
    if diff <= -5:
        return "declining"
    return "stable"


def _days_to(date_str: Optional[str]) -> Optional[int]:
    if not date_str or not isinstance(date_str, str):
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(date_str[:len(fmt) + 6], fmt)
            break
        except (ValueError, TypeError):
            continue
    else:
        try:
            dt = datetime.fromisoformat(date_str.rstrip("Z"))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt.date() - datetime.now(timezone.utc).date()
    return delta.days


def _latest_activity_by_kind(
    activities: Optional[list[dict[str, Any]]],
) -> dict[str, Optional[str]]:
    """Return ``{last_meeting_at, last_email_at, last_touch_at}``."""
    out: dict[str, Optional[str]] = {
        "last_meeting_at": None,
        "last_email_at": None,
        "last_touch_at": None,
    }
    if not activities:
        return out
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        kind = (activity.get("type") or activity.get("kind") or "").lower()
        date = _first_str(
            activity.get("date"),
            activity.get("activityDate"),
            activity.get("createdAt"),
            activity.get("performedAt"),
        )
        if not date:
            continue
        if out["last_touch_at"] is None or date > (out["last_touch_at"] or ""):
            out["last_touch_at"] = date
        if any(k in kind for k in ("meeting", "call", "demo", "qbr")) and (
            out["last_meeting_at"] is None or date > (out["last_meeting_at"] or "")
        ):
            out["last_meeting_at"] = date
        elif "email" in kind and (
            out["last_email_at"] is None or date > (out["last_email_at"] or "")
        ):
            out["last_email_at"] = date
    return out


def _build_summary(
    *,
    account_id: str,
    account_name: str,
    planhat_id: str,
    match_method: str,
    company: Any,
    health: Any,
    nps: Any,
    activities: Any,
    tasks: Any,
    conversations: Any,
) -> dict[str, Any]:
    company = company if isinstance(company, dict) else {}
    health_obj = health if isinstance(health, dict) else {}
    nps_obj = nps if isinstance(nps, dict) else {}
    activities_list = activities if isinstance(activities, list) else (
        activities.get("data") if isinstance(activities, dict) else None
    )
    tasks_list = tasks if isinstance(tasks, list) else (
        tasks.get("data") if isinstance(tasks, dict) else None
    )
    convo_list = conversations if isinstance(conversations, list) else (
        conversations.get("data") if isinstance(conversations, dict) else None
    )

    # Health
    health_score = _first_number(
        health_obj.get("score"),
        health_obj.get("healthScore"),
        health_obj.get("current"),
        company.get("healthScore"),
        company.get("health"),
    )
    prev_health = _first_number(
        health_obj.get("previous"),
        health_obj.get("previousScore"),
        health_obj.get("priorScore"),
    )
    trend_label = _first_str(
        health_obj.get("trend"),
        company.get("healthTrend"),
    )
    health_trend = trend_label.lower() if trend_label else _classify_trend(
        health_score, prev_health,
    )
    if health_score is not None:
        health_score = round(max(0.0, min(100.0, health_score)), 1)

    # Lifecycle
    lifecycle_phase = _first_str(
        company.get("phase"),
        company.get("lifecyclePhase"),
        company.get("status"),
        company.get("stage"),
    )

    # NPS
    nps_responses = nps_obj.get("responses") or nps_obj.get("data")
    if isinstance(nps_responses, list) and nps_responses:
        latest = nps_responses[0] if isinstance(nps_responses[0], dict) else {}
    else:
        latest = nps_obj
    nps_score = _first_number(
        latest.get("score"),
        latest.get("npsScore"),
        nps_obj.get("score"),
        company.get("npsScore"),
    )
    nps_responded_at = _first_str(
        latest.get("respondedAt"),
        latest.get("createdAt"),
        latest.get("date"),
        nps_obj.get("respondedAt"),
        company.get("lastNps"),
    )

    # Renewal + ARR
    renewal_date = _first_str(
        company.get("renewalDate"),
        company.get("nextRenewalDate"),
        company.get("contractEnd"),
        company.get("renewal_date"),
    )
    arr = _first_number(
        company.get("arr"),
        company.get("annualRevenue"),
        company.get("currentRevenue"),
    )
    mrr = _first_number(
        company.get("mrr"),
        company.get("monthlyRevenue"),
    )
    churn_flag = bool(
        company.get("churnRisk")
        or company.get("atRisk")
        or company.get("isAtRisk")
        or (lifecycle_phase or "").lower() in {"churned", "at risk", "at-risk"}
    )

    # Recent engagement
    touch_dates = _latest_activity_by_kind(activities_list)

    # Compact open tasks/conversations
    open_tasks = _compact_tasks(tasks_list)
    open_convos = _compact_conversations(convo_list)

    custom_fields = company.get("customFields") or company.get("custom") or {}
    if not isinstance(custom_fields, dict):
        custom_fields = {}

    return {
        "account_id": account_id,
        "account_name": account_name,
        "source": _planhat_source_label(),
        "planhat_company_id": planhat_id,
        "match_method": match_method,
        "health_score": health_score,
        "health_trend": health_trend,
        "lifecycle_phase": lifecycle_phase,
        "nps_score": nps_score,
        "nps_responded_at": nps_responded_at,
        "renewal_date": renewal_date,
        "days_to_renewal": _days_to(renewal_date),
        "arr": arr,
        "mrr": mrr,
        "churn_risk_flag": churn_flag,
        "last_meeting_at": touch_dates["last_meeting_at"],
        "last_email_at": touch_dates["last_email_at"],
        "last_touch_at": touch_dates["last_touch_at"],
        "open_tasks": open_tasks,
        "open_conversations": open_convos,
        "custom_fields": custom_fields,
        "raw": {
            "company_keys": sorted(company.keys()) if isinstance(company, dict) else [],
        },
    }


def _compact_tasks(tasks: Any) -> list[dict[str, Any]]:
    if not isinstance(tasks, list):
        return []
    out: list[dict[str, Any]] = []
    for task in tasks[:_TASK_LIMIT]:
        if not isinstance(task, dict):
            continue
        out.append({
            "id": _first_str(task.get("_id"), task.get("id")) or "",
            "title": _first_str(task.get("title"), task.get("name"), task.get("subject")) or "",
            "due": _first_str(task.get("dueDate"), task.get("due"), task.get("deadline")),
            "status": _first_str(task.get("status"), task.get("state")) or "open",
        })
    return out


def _compact_conversations(convos: Any) -> list[dict[str, Any]]:
    if not isinstance(convos, list):
        return []
    out: list[dict[str, Any]] = []
    for conv in convos[:_CONVO_LIMIT]:
        if not isinstance(conv, dict):
            continue
        out.append({
            "id": _first_str(conv.get("_id"), conv.get("id")) or "",
            "subject": _first_str(conv.get("subject"), conv.get("title"), conv.get("topic")) or "",
            "sentiment": _first_str(conv.get("sentiment"), conv.get("sentimentLabel")),
            "type": _first_str(conv.get("type"), conv.get("kind")),
            "date": _first_str(
                conv.get("date"), conv.get("updatedAt"), conv.get("createdAt"),
            ),
        })
    return out
