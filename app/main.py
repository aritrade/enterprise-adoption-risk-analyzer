"""
Adoption & Escalation Risk Analyzer — FastAPI application.
Combines Salesforce case data, Nutanix Insights cluster health,
and CS Insights customer-success metrics into a unified risk dashboard.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import settings
from app.engine.aggregator import Aggregator
from app.engine.region_map import REGIONS, is_valid_region
from app.store.vector_store import get_vector_store
from app.sync.ai_heal import is_ollama_ready

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

aggregator = Aggregator()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Adoption & Escalation Risk Analyzer starting up")
    asyncio.create_task(aggregator.prewarm())
    yield
    logger.info("Shutting down — closing browser bridge...")
    await aggregator.shutdown()
    logger.info("Shutdown complete")


app = FastAPI(
    title="Adoption & Escalation Risk Analyzer",
    description="Unified escalation-risk scoring across Salesforce, Nutanix Insights, CS Insights, and Glean",
    version="1.0.0",
    lifespan=lifespan,
)

# Anchor static/template dirs to this file so the app works regardless of the
# process working directory (local `cd` vs. platform start command).
_BASE_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _BASE_DIR / "static"
_TEMPLATES_DIR = _BASE_DIR / "templates"

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
def _make_cache_bust() -> str:
    """Hash of key static assets so browsers re-fetch after any edit."""
    h = hashlib.md5()
    for rel in ("js/app.js", "css/style.css"):
        try:
            h.update(str(os.path.getmtime(_STATIC_DIR / rel)).encode())
        except OSError:
            h.update(rel.encode())
    return h.hexdigest()[:10]

_CACHE_BUST = _make_cache_bust()
templates.env.globals["cache_bust"] = _CACHE_BUST


# ── Dashboard ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html")


# ── API: Account analysis ────────────────────────────────────────────────

@app.get("/api/account/{account_id}")
async def analyze_account(
    account_id: str,
    account_name: str = Query("Unknown"),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    try:
        return await aggregator.analyze_account(
            account_id, account_name, bypass_cache=refresh
        )
    except Exception as exc:
        logger.exception("Error analysing account %s", account_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


_REGION_PATTERN = r"^(APJ|EMEA|AMER|Unknown|all)$"


def _normalise_region(region: Optional[str]) -> Optional[str]:
    """``"all"`` / ``None`` => no filter; everything else must be a known region."""
    if not region or region == "all":
        return None
    if not is_valid_region(region):
        raise HTTPException(status_code=400, detail=f"invalid region: {region}")
    return region


@app.get("/api/top-risks")
async def top_risks(
    limit: int = Query(20, ge=1, le=100),
    region: Optional[str] = Query(None, pattern=_REGION_PATTERN),
) -> list[dict[str, Any]]:
    try:
        return await aggregator.get_top_risk_accounts(
            limit=limit, region=_normalise_region(region),
        )
    except Exception as exc:
        logger.exception("Error fetching top risks")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/search")
async def search_accounts(q: str = Query(..., min_length=2)) -> list[dict[str, Any]]:
    try:
        return await aggregator.search_and_analyze(q)
    except Exception as exc:
        logger.exception("Error searching accounts")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/accounts/search")
async def fast_search_accounts(
    q: str = Query(..., min_length=2),
    limit: int = Query(50, ge=1, le=200),
    region: Optional[str] = Query(None, pattern=_REGION_PATTERN),
) -> list[dict[str, Any]]:
    """Sub-millisecond in-memory search across ALL customer accounts."""
    return aggregator.search_accounts_fast(
        q, limit=limit, region=_normalise_region(region),
    )


@app.get("/api/accounts")
async def list_accounts(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    region: Optional[str] = Query(None, pattern=_REGION_PATTERN),
) -> dict[str, Any]:
    """Paginated list of all customer accounts from the in-memory index."""
    return aggregator.get_all_accounts_page(
        offset=offset, limit=limit, region=_normalise_region(region),
    )


@app.get("/api/accounts/stats")
async def account_stats() -> dict[str, Any]:
    return aggregator.get_account_index_stats()


@app.get("/api/regions/counts")
async def region_counts() -> dict[str, Any]:
    """Per-region account counts; powers the chip-bar badge numbers."""
    counts = aggregator.get_region_counts()
    total = sum(counts.values())
    return {
        "counts": {r: int(counts.get(r, 0)) for r in REGIONS},
        "total":  total,
    }


@app.get("/api/cxm/roster")
async def cxm_roster(
    region: Optional[str] = Query(None, pattern=_REGION_PATTERN),
) -> list[dict[str, Any]]:
    return aggregator.get_cxm_roster(region=_normalise_region(region))


@app.get("/api/accounts/by-cxm")
async def accounts_by_cxm(
    cxm: str = Query(..., min_length=2),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    region: Optional[str] = Query(None, pattern=_REGION_PATTERN),
) -> dict[str, Any]:
    return aggregator.get_accounts_by_cxm(
        cxm, offset=offset, limit=limit, region=_normalise_region(region),
    )




# ── API: Individual connector data (for drill-down) ─────────────────────

@app.get("/api/salesforce/cases/{account_id}")
async def sf_cases(account_id: str) -> dict[str, Any]:
    if not aggregator.sf:
        raise HTTPException(status_code=503, detail="Salesforce connector not configured")
    try:
        return await asyncio.to_thread(aggregator.sf.get_account_summary, account_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/insights/health/{account_name}")
async def insights_health(
    account_name: str,
    account_id: str = Query(""),
) -> dict[str, Any]:
    if not aggregator.insights:
        from app.connectors.demo_data import generate_insights_health
        return generate_insights_health(account_name)
    try:
        return await aggregator.insights.get_account_health_summary(
            account_name, account_id=account_id
        )
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/csinsights/{account_id}")
async def cs_insights(
    account_id: str,
    account_name: str = Query(""),
) -> dict[str, Any]:
    if not aggregator.csinsights:
        from app.connectors.demo_data import generate_cs_summary
        return generate_cs_summary(account_id)
    try:
        return await aggregator.csinsights.get_account_cs_summary(
            account_id, account_name=account_name
        )
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── API: Glean (internal engagement signal) ───────────────────────────────

@app.get("/api/glean/{account_id}")
@app.get("/api/engagement/{account_id}")  # legacy alias — kept for back-compat
async def glean_summary(
    account_id: str,
    account_name: str = Query(""),
) -> dict[str, Any]:
    if not aggregator.engagement:
        from app.connectors.demo_data import generate_engagement_summary
        return generate_engagement_summary(account_id, account_name or "Unknown")
    try:
        return await aggregator.engagement.get_account_engagement_summary(
            account_id, account_name=account_name
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── API: Infrastructure summary ──────────────────────────────────────────

@app.get("/api/infrastructure/{account_id}")
async def infrastructure_summary(account_id: str) -> dict[str, Any]:
    if not aggregator.sf:
        raise HTTPException(status_code=503, detail="Salesforce connector not configured")
    try:
        return await asyncio.to_thread(aggregator.sf.get_infrastructure_summary, account_id)
    except Exception as exc:
        logger.exception("Error fetching infrastructure for %s", account_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── API: Account Financials ───────────────────────────────────────────────

_ADVISORY_CACHE: dict[str, dict[str, Any]] = {}


@app.get("/api/account/{account_id}/ai-advisory")
async def ai_advisory(
    account_id: str,
    account_name: str = Query("Unknown"),
) -> dict[str, Any]:
    """Claude-generated action plan for one account (deterministic fallback).

    Cached per account so Claude is called at most once per account — cost
    stays negligible. The API key is read server-side only.
    """
    if account_id in _ADVISORY_CACHE:
        return _ADVISORY_CACHE[account_id]
    try:
        profile = await aggregator.analyze_account(account_id, account_name)
        from app.engine.claude_advisor import generate_advisory
        result = await generate_advisory(profile)
        _ADVISORY_CACHE[account_id] = result
        return result
    except Exception as exc:
        logger.exception("AI advisory failed for %s", account_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/account/{account_id}/financials")
async def account_financials(account_id: str) -> dict[str, Any]:
    try:
        return await aggregator.get_account_financials(account_id)
    except Exception as exc:
        logger.exception("Error fetching financials for %s", account_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── API: License / Adoption analysis ─────────────────────────────────────

@app.get("/api/licenses/{account_id}")
async def license_analysis(
    account_id: str,
    account_name: str = Query(""),
) -> dict[str, Any]:
    try:
        return await aggregator.get_license_analysis(account_id, account_name)
    except Exception as exc:
        logger.exception("Error fetching license data for %s", account_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── API: Email draft & sending ────────────────────────────────────────────


class SendEmailRequest(BaseModel):
    to: list[str]
    cc: list[str] = []
    subject: str
    body_html: str
    token: Optional[str] = None


class CreateDraftRequest(BaseModel):
    to: list[str]
    cc: list[str] = []
    subject: str
    body_html: str
    token: Optional[str] = None


@app.get("/api/account/{account_id}/email-draft")
async def email_draft(
    account_id: str,
    account_name: str = Query("Unknown"),
    sender_name: str = Query(""),
    include_license: bool = Query(False),
) -> dict[str, Any]:
    try:
        return await aggregator.generate_email_draft(
            account_id, account_name,
            sender_name=sender_name,
            include_license=include_license,
        )
    except Exception as exc:
        logger.exception("Error generating email draft for %s", account_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/account/{account_id}/contacts")
async def account_contacts(
    account_id: str,
    account_name: str = Query(""),
) -> list[dict[str, Any]]:
    try:
        return await aggregator.get_account_contacts(account_id, account_name)
    except Exception as exc:
        logger.exception("Error fetching contacts for %s", account_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/email/send")
async def send_email(req: SendEmailRequest) -> dict[str, Any]:
    try:
        return await aggregator.send_email_via_outlook(
            to=req.to, cc=req.cc,
            subject=req.subject, body_html=req.body_html,
            token=req.token,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Error sending email")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/email/draft")
async def create_email_draft_outlook(req: CreateDraftRequest) -> dict[str, Any]:
    try:
        return await aggregator.create_outlook_draft(
            to=req.to, cc=req.cc,
            subject=req.subject, body_html=req.body_html,
            token=req.token,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Error creating Outlook draft")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/outlook/status")
async def outlook_status() -> dict[str, Any]:
    connector = aggregator.outlook
    result: dict[str, Any] = {
        "enabled": connector.enabled,
        "mode": connector.mode,
    }
    if connector.mode in ("outlook_app", "power_automate"):
        result["ready"] = True
        result["auth_url"] = None
    elif connector.mode == "ms_graph":
        result["ready"] = False
        result["auth_url"] = connector.get_auth_url("era")
    else:
        result["ready"] = False
        result["auth_url"] = None
    return result


@app.get("/auth/callback")
async def outlook_auth_callback(
    code: str = Query(""),
    error: str = Query(""),
    state: str = Query(""),
):
    if error:
        return HTMLResponse(
            f"<h3>Authentication failed</h3><p>{error}</p>"
            f'<p><a href="/">Back to dashboard</a></p>',
            status_code=400,
        )
    if not code:
        raise HTTPException(status_code=400, detail="No authorization code received")
    try:
        result = await aggregator.outlook.exchange_code(code)
        user = result.get("user", {})
        name = user.get("display_name", "User")
        return HTMLResponse(f"""<!DOCTYPE html>
<html><body style="font-family:sans-serif;background:#0f1923;color:#e8edf2;display:flex;align-items:center;justify-content:center;height:100vh">
<div style="text-align:center">
    <h2 style="color:#2ed573">Outlook Connected</h2>
    <p>Signed in as <strong>{name}</strong></p>
    <p>You can now send emails directly from the Adoption & Escalation Risk Analyzer.</p>
    <p><a href="/" style="color:#3b9eff">Back to Dashboard</a></p>
    <script>
        if (window.opener) {{
            window.opener.postMessage({{type: 'outlook_auth_success', user: '{name}'}}, '*');
            setTimeout(() => window.close(), 2000);
        }}
    </script>
</div>
</body></html>""")
    except Exception as exc:
        logger.exception("Outlook auth callback failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── API: My Accounts (TAM portfolio) ─────────────────────────────────────

@app.get("/api/my-accounts")
async def my_accounts(
    username: str = Query(..., min_length=2),
    limit: int = Query(50, ge=1, le=200),
) -> list[dict[str, Any]]:
    try:
        return await aggregator.get_my_accounts(username, limit=limit)
    except Exception as exc:
        logger.exception("Error fetching my-accounts for %s", username)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── API: CSV Export ──────────────────────────────────────────────────────

@app.get("/api/export/csv")
async def export_csv(
    q: str = Query("", min_length=0),
    limit: int = Query(100, ge=1, le=500),
):
    if q:
        accounts = await aggregator.search_and_analyze(q)
    else:
        accounts = await aggregator.get_top_risk_accounts(limit=limit)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Account Name", "Account ID", "Risk Score", "Risk Level",
        "Open Cases", "P1 Cases", "P2 Cases", "Escalated",
        "Critical Alerts", "Clusters", "Nodes", "EOL Nodes",
        "AHV Nodes", "ESXi Nodes", "Primary HW",
        "Contract Status", "Pulse Enabled",
        "CS Health", "Engagement", "Renewal Risk",
        "Internal Touchpoints", "Recent Activity (30d)", "Escalation Refs",
        "Account Owner", "Systems Engineer", "Region", "Theater", "Vertical",
        "Last Sale Date", "Top Recommendations",
    ])
    for a in accounts:
        sf = a.get("salesforce", {})
        ins = a.get("insights", {})
        cs = a.get("cs_insights", {})
        gl = a.get("engagement", a.get("glean", {}))
        meta = cs.get("account_metadata", {})
        recs = " | ".join(a.get("recommendations", [])[:3])
        hv = ins.get("hypervisor_distribution", {})
        hw = ins.get("hw_partner_distribution", {})
        top_hw = max(hw.items(), key=lambda x: x[1])[0] if hw else ""
        writer.writerow([
            a.get("account_name", ""),
            a.get("account_id", ""),
            a.get("overall_score", ""),
            a.get("risk_level", ""),
            sf.get("open_cases", ""),
            sf.get("p1_cases", ""),
            sf.get("p2_cases", ""),
            sf.get("escalated_cases", ""),
            ins.get("total_critical_alerts", ""),
            ins.get("total_clusters", ""),
            ins.get("total_nodes", ""),
            ins.get("eol_exposure_count", ""),
            hv.get("AHV", ""),
            hv.get("ESXI", hv.get("ESXi", "")),
            top_hw,
            ins.get("contract_status", ""),
            ins.get("pulse_enabled", ""),
            cs.get("health_score", ""),
            cs.get("engagement_score", ""),
            cs.get("renewal_risk", ""),
            gl.get("total_mentions", ""),
            gl.get("recent_mentions_30d", ""),
            gl.get("escalation_mentions", ""),
            meta.get("account_owner", ""),
            meta.get("systems_engineer", ""),
            meta.get("region", ""),
            meta.get("theater", ""),
            meta.get("vertical", ""),
            meta.get("most_recent_sale", ""),
            recs,
        ])

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=escalation-risk-report.csv"},
    )


# ── API: CXM/TAM Custom Risk Factors ─────────────────────────────────────


class CustomRiskCreate(BaseModel):
    category: str
    severity: str = "medium"
    title: str = ""
    notes: str = ""
    created_by: str = ""


class CustomRiskUpdate(BaseModel):
    severity: Optional[str] = None
    title: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None


@app.get("/api/risk-categories")
async def risk_categories() -> list[dict[str, str]]:
    return aggregator.custom_risk_store.get_categories()


@app.get("/api/account/{account_id}/custom-risks")
async def list_custom_risks(account_id: str) -> list[dict]:
    return aggregator.custom_risk_store.list_for_account(account_id)


@app.post("/api/account/{account_id}/custom-risks")
async def add_custom_risk(account_id: str, body: CustomRiskCreate) -> dict:
    entry = aggregator.custom_risk_store.add(
        account_id=account_id,
        category=body.category,
        severity=body.severity,
        title=body.title,
        notes=body.notes,
        created_by=body.created_by,
    )
    aggregator.clear_cache()
    return entry


@app.put("/api/account/{account_id}/custom-risks/{risk_id}")
async def update_custom_risk(
    account_id: str, risk_id: str, body: CustomRiskUpdate
) -> dict:
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    result = aggregator.custom_risk_store.update(account_id, risk_id, updates)
    if result is None:
        raise HTTPException(status_code=404, detail="Risk flag not found")
    aggregator.clear_cache()
    return result


@app.delete("/api/account/{account_id}/custom-risks/{risk_id}")
async def delete_custom_risk(account_id: str, risk_id: str) -> dict[str, bool]:
    ok = aggregator.custom_risk_store.delete(account_id, risk_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Risk flag not found")
    aggregator.clear_cache()
    return {"deleted": True}


# ── API: Risk pie chart (aggregated weighted scores per source) ─────────

@app.get("/api/account/{account_id}/risk-breakdown")
async def account_risk_breakdown(account_id: str) -> dict[str, Any]:
    """
    Pie-chart slices for a single account — each slice is the sum of
    ``factors[].weighted_score`` grouped by ``factors[].source``.
    """
    return aggregator.get_account_risk_breakdown(account_id)


@app.get("/api/portfolio/risk-breakdown")
async def portfolio_risk_breakdown(
    level: str = Query("high", pattern="^(high|medium|all)$"),
) -> dict[str, Any]:
    """
    Pie-chart slices summed across every account at-or-above ``level``,
    plus the top-10 accounts contributing most to each slice.
    """
    return aggregator.get_portfolio_risk_breakdown(level=level)


# ── API: Sync observability and manual triggers ─────────────────────────

@app.get("/api/sync/runs")
async def sync_runs(limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    """Recent ingestion runs across all sources, newest first."""
    return aggregator.db.list_sync_runs(limit=limit)


@app.get("/api/sync/freshness")
async def sync_freshness() -> dict[str, Any]:
    """Most-recent successful run per source (for staleness badges)."""
    latest = aggregator.db.latest_run_per_source()
    snapshot_freshness = aggregator.db.source_freshness()
    return {
        "latest_runs": latest,
        "snapshot_freshness": snapshot_freshness,
        "stale_after_hours": settings.sync_stale_after_hours,
        "interval_hours": settings.sync_interval_hours,
    }


@app.post("/api/sync/run/{source}")
async def sync_run_source(source: str) -> dict[str, Any]:
    """Trigger an immediate batch sync for one source (no wait)."""
    if source not in (
        "salesforce", "insights", "csinsights", "planhat", "glean", "portal_globals",
    ):
        raise HTTPException(status_code=400, detail="unknown source")
    asyncio.create_task(aggregator.scheduler.sync_source_now(source))
    return {"queued": True, "source": source}


@app.post("/api/sync/account/{account_id}")
async def sync_one_account(
    account_id: str, account_name: str = Query(""),
) -> dict[str, Any]:
    """Refresh every source for one account synchronously."""
    if not account_name:
        acct = aggregator.db.get_account(account_id)
        account_name = acct["name"] if acct else "Unknown"
    return await aggregator.scheduler.sync_account_now(account_id, account_name)


@app.get("/sync", response_class=HTMLResponse)
async def sync_page(request: Request) -> Any:
    return templates.TemplateResponse(request, "sync.html")


# ── API: Cache management ────────────────────────────────────────────────

@app.post("/api/cache/clear")
async def clear_cache() -> dict[str, Any]:
    cleared = aggregator.clear_cache()
    return {"cleared": cleared}


# ── API: Semantic search (ChromaDB) ──────────────────────────────────────

_SEMANTIC_SOURCES = ("salesforce", "insights", "csinsights", "planhat", "glean", "all")


def _resolve_semantic_source(source: Optional[str]) -> Optional[str]:
    """``None`` / ``"all"`` => unified collection. Otherwise validate."""
    if not source or source == "all":
        return None
    if source not in _SEMANTIC_SOURCES:
        raise HTTPException(
            status_code=400, detail=f"unknown source: {source}",
        )
    return source


def _attach_account_metadata(
    hits: list[dict[str, Any]],
    region_filter: Optional[str],
) -> list[dict[str, Any]]:
    """Hydrate hits with ``region`` from the SQLite account index, optionally
    filtering by region."""
    out: list[dict[str, Any]] = []
    for hit in hits:
        aid = hit.get("account_id") or ""
        if not aid:
            continue
        acct = aggregator.db.get_account(aid)
        region = (acct or {}).get("region") or ""
        if region_filter and region != region_filter:
            continue
        hit["region"] = region
        if acct and not hit.get("account_name"):
            hit["account_name"] = acct.get("name", "")
        out.append(hit)
    return out


@app.get("/api/search/semantic")
async def search_semantic(
    q: str = Query(..., min_length=2),
    source: Optional[str] = Query(None),
    k: int = Query(20, ge=1, le=100),
    region: Optional[str] = Query(None, pattern=_REGION_PATTERN),
) -> dict[str, Any]:
    """
    Semantic search across the ChromaDB-indexed connector snapshots.

    `source=all` (default) searches every source's snapshot; pass an
    individual source name (`planhat`, `salesforce`, …) to scope the
    query. `region` filters the result set against the SQLite account
    index after the vector search.
    """
    store = get_vector_store()
    if not store.available:
        raise HTTPException(
            status_code=503,
            detail="Vector store unavailable — install chromadb and run "
                   "`make reindex` to populate it.",
        )
    src = _resolve_semantic_source(source)
    region_filter = _normalise_region(region)
    # Over-fetch a little so the post-filter still produces ``k`` rows.
    raw = store.search(q, source=src, k=k * 2 if region_filter else k)
    hits = _attach_account_metadata(raw, region_filter=region_filter)
    return {
        "query": q,
        "source": src or "all",
        "region": region_filter or "all",
        "total": len(hits),
        "hits": hits[:k],
    }


@app.get("/api/account/{account_id}/similar")
async def account_similar(
    account_id: str,
    source: str = Query("planhat", pattern="^(salesforce|insights|csinsights|planhat|glean)$"),
    k: int = Query(10, ge=1, le=50),
) -> dict[str, Any]:
    """
    Find accounts whose ``source`` snapshot reads similarly to this
    account's. Useful for "show me other customers that look like this
    Planhat profile" workflows.
    """
    store = get_vector_store()
    if not store.available:
        raise HTTPException(
            status_code=503,
            detail="Vector store unavailable — install chromadb and run "
                   "`make reindex` to populate it.",
        )
    raw = store.similar_to(source, account_id, k=k)
    hits = _attach_account_metadata(raw, region_filter=None)
    return {
        "anchor_account_id": account_id,
        "source": source,
        "total": len(hits),
        "hits": hits,
    }


# ── Status ───────────────────────────────────────────────────────────────

@app.get("/api/status")
async def status() -> dict[str, Any]:
    vs = get_vector_store()
    return {
        "status": "ok",
        "demo_mode": aggregator._demo,
        "connectors": aggregator.connector_status,
        "sync": {
            "interval_hours": settings.sync_interval_hours,
            "enabled": settings.sync_enabled,
            "ai_heal": settings.ai_heal_enabled and is_ollama_ready(),
        },
        "vector_store": {
            "available": vs.available,
            "embed_model": vs.embed_model,
            "path": str(vs.persist_directory),
        },
        "event_bus": {
            "enabled": settings.event_bus_enabled,
            "backend": settings.event_bus_backend,
        },
    }


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
