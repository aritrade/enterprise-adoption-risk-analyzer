"""
APScheduler-driven background sync.

Two operating modes share one code path:

* **In-process (default, dev)** — :class:`Scheduler` is started from
  :func:`app.engine.aggregator.Aggregator.prewarm` so a single
  ``python run.py`` runs both the FastAPI app and the scheduler.
* **Standalone container (prod)** — ``python -m app.sync.scheduler``
  starts a long-lived process with no FastAPI server.

Cron layout (staggered 20 minutes apart so we never burst the same
backend at once):

    HH:00 — Salesforce index + per-account sync
    HH:20 — Nutanix Insights per-account sync
    HH:40 — CS Insights per-account sync
    HH+1:00 — Glean (internal engagement) per-account sync

After every per-source sync, the scheduler recomputes risk profiles and
pie-chart aggregates from the snapshots already in the DB — pure
compute, no external calls.

The watch list (which accounts to refresh each cycle) is, in priority:
1. Accounts with an existing risk profile (already-tracked customers).
2. Accounts with active CXM custom risks.
3. Accounts with escalated cases (Salesforce live query, top of the list).
4. Top-N from the last cycle.

Capped by ``settings.sync_max_accounts_per_run`` (0 == no cap).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from datetime import datetime, timezone
from typing import Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.connectors.csinsights import CSInsightsConnector
from app.connectors.glean import InternalEngagementConnector
from app.connectors.nutanix_insights import NutanixInsightsConnector
from app.connectors.planhat import PlanhatConnector
from app.connectors.salesforce import SalesforceConnector
from app.engine.risk_scorer import RiskScorer
from app.store.custom_risks import CustomRiskStore
from app.store.db import RiskProfileRow, get_db
from app.store.vector_store import get_vector_store
from app.sync.base import WorkerContext, WorkerResult
from app.sync.event_bus import build_event_bus
from app.sync.csinsights_sync import CSInsightsWorker
from app.sync.glean_sync import GleanWorker
from app.sync.insights_sync import InsightsWorker
from app.sync.planhat_sync import PlanhatWorker
from app.sync.portal_globals_sync import PortalGlobalsWorker
from app.sync.salesforce_sync import SalesforceWorker

logger = logging.getLogger(__name__)


# ── Slot definitions ────────────────────────────────────────────────

_SOURCE_CRON_MINUTES = {
    "salesforce": 0,
    "portal_globals": 10,  # before per-account insights so the cache is fresh
    "insights": 20,
    "planhat": 30,  # sits between insights and CS Insights to stagger load
    "csinsights": 40,
    "glean": 50,  # within the same hour to keep cycle <= 2h
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Risk profile recomputation ──────────────────────────────────────

class ProfileRecomputer:
    """
    Builds an :class:`AccountRiskProfile` from the snapshots already in
    the DB. Pure compute, no I/O against external sources.
    """

    def __init__(self, scorer: RiskScorer, custom_risks: CustomRiskStore) -> None:
        self.scorer = scorer
        self.custom_risks = custom_risks
        self.db = get_db()

    def recompute(self, account_id: str, account_name: str) -> Optional[dict[str, Any]]:
        snaps = self.db.get_all_source_snapshots(account_id)
        sf = snaps.get("salesforce") or {}
        ins = snaps.get("insights") or {}
        cs = dict(snaps.get("csinsights") or {})
        glean = snaps.get("glean") or {}
        planhat = snaps.get("planhat") or {}

        sf_summary = sf.get("summary", {}) if isinstance(sf, dict) else {}
        sf_renewal = sf.get("renewal", {}) if isinstance(sf, dict) else {}

        # Apply the same SF-renewal override that the legacy aggregator did,
        # so the CS bucket carries the authoritative renewal fields.
        if sf_renewal and sf_renewal.get("has_renewal"):
            cs["renewal_date"] = sf_renewal.get("renewal_date")
            cs["days_to_renewal"] = sf_renewal.get("days_to_renewal")
            cs["renewal_risk_score"] = sf_renewal.get("renewal_risk_score")
            cs["renewal_risk_label"] = sf_renewal.get("renewal_risk_label", "unknown")
            cs["renewal_source"] = "salesforce"
            cs["contract_value"] = sf_renewal.get("contract_value", 0)
            cs["total_renewal_value"] = sf_renewal.get("total_renewal_value", 0)
            cs["renewal_count"] = sf_renewal.get("renewal_count", 0)
            cs["renewals"] = sf_renewal.get("renewals", [])

        custom = self.custom_risks.list_for_account(account_id)
        try:
            profile = self.scorer.score_account(
                account_id=account_id,
                account_name=account_name,
                sf_data=sf_summary,
                insights_data=ins,
                cs_data=cs,
                custom_risks=custom,
                glean_data=glean,
                planhat_data=planhat,
            )
        except Exception:
            logger.exception(
                "Risk recompute failed for %s; skipping profile write", account_id
            )
            return None

        result = profile.to_dict()
        # Track per-source freshness so the UI can show staleness badges.
        result["data_freshness"] = {
            src: payload.get("_fetched_at")
            for src, payload in snaps.items()
            if isinstance(payload, dict)
        }
        self.db.upsert_risk_profile(account_id, account_name, result)
        return result


# ── Pie chart aggregate recomputation ───────────────────────────────

_PIE_SOURCES = ("salesforce", "insights", "csinsights", "glean", "cxm")


def _slices_from_profile(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Group the profile's RiskFactors by their ``source`` for the pie chart."""
    slices: dict[str, dict[str, Any]] = {
        s: {"source": s, "weighted_total": 0.0, "top_factors": [], "account_count": 1}
        for s in _PIE_SOURCES
    }
    for f in profile.get("factors", []):
        src = f.get("source") or "other"
        bucket = slices.setdefault(
            src,
            {"source": src, "weighted_total": 0.0, "top_factors": [], "account_count": 1},
        )
        bucket["weighted_total"] += float(f.get("weighted_score") or 0)
        bucket["top_factors"].append({
            "category": f.get("category", ""),
            "description": f.get("description", ""),
            "weighted_score": f.get("weighted_score", 0),
        })
    for s in slices.values():
        s["top_factors"] = sorted(
            s["top_factors"], key=lambda f: f["weighted_score"], reverse=True,
        )[:5]
        s["weighted_total"] = round(s["weighted_total"], 2)
    return [s for s in slices.values() if s["weighted_total"] > 0]


class AggregateRecomputer:
    """Compute and persist pie-chart slices for one account and the portfolio."""

    def __init__(self) -> None:
        self.db = get_db()

    def recompute_account(self, account_id: str, profile: dict[str, Any]) -> None:
        slices = _slices_from_profile(profile)
        self.db.replace_aggregates("account", account_id, slices)

    def recompute_portfolio(self, level: str = "high") -> None:
        """
        Sum weighted scores across accounts at-or-above the given risk
        level and emit one row per source, plus the top-10 accounts that
        contribute most to each slice.
        """
        threshold = (
            settings.risk_threshold_high if level == "high"
            else settings.risk_threshold_medium if level == "medium"
            else 0
        )
        bins: dict[str, dict[str, Any]] = {
            s: {
                "source": s, "weighted_total": 0.0,
                "top_factors": [], "top_accounts_raw": [],
                "account_count": 0,
            }
            for s in _PIE_SOURCES
        }
        for row in self.db.iter_risk_profiles():
            if row.overall_score < threshold:
                continue
            for s in _slices_from_profile(row.profile):
                src = s["source"]
                bucket = bins.setdefault(
                    src, {"source": src, "weighted_total": 0.0,
                          "top_factors": [], "top_accounts_raw": [],
                          "account_count": 0},
                )
                bucket["weighted_total"] += s["weighted_total"]
                bucket["account_count"] += 1
                bucket["top_accounts_raw"].append({
                    "account_id": row.account_id,
                    "account_name": row.account_name,
                    "overall_score": row.overall_score,
                    "source_score": s["weighted_total"],
                })

        slices = []
        for s in bins.values():
            top_accts = sorted(
                s.pop("top_accounts_raw"),
                key=lambda a: a["source_score"], reverse=True,
            )[:10]
            s["top_accounts"] = top_accts
            s["weighted_total"] = round(s["weighted_total"], 2)
            if s["weighted_total"] > 0:
                slices.append(s)

        self.db.replace_aggregates("portfolio", level, slices)


# ── Scheduler ───────────────────────────────────────────────────────

class Scheduler:
    """
    Owns the per-source workers and the APScheduler instance.

    Lifecycle:
      ``Scheduler(...)`` → ``await start()`` → … → ``await shutdown()``
    """

    def __init__(self, ctx: WorkerContext) -> None:
        self.ctx = ctx
        self.db = ctx.db
        self.scorer = RiskScorer()
        self.custom_risks = CustomRiskStore()
        self.recomputer = ProfileRecomputer(self.scorer, self.custom_risks)
        self.aggregator_recompute = AggregateRecomputer()
        self.vector_store = get_vector_store()
        self.event_bus = build_event_bus()
        self.workers = {
            "salesforce": SalesforceWorker(ctx),
            "insights": InsightsWorker(ctx),
            "csinsights": CSInsightsWorker(ctx),
            "planhat": PlanhatWorker(ctx),
            "glean": GleanWorker(ctx),
        }
        # Portal globals don't fit the per-account BaseWorker shape — they
        # produce one cache update per cycle, not per-account snapshots —
        # so the scheduler treats them as a special source with its own
        # entry point. Still gets a cron slot and a sync_runs row.
        self.globals_worker = PortalGlobalsWorker(ctx)
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._account_locks: dict[str, asyncio.Lock] = {}
        self._source_locks: dict[str, asyncio.Lock] = {
            s: asyncio.Lock() for s in self.workers
        }
        self._source_locks["portal_globals"] = asyncio.Lock()

    # ── Lifecycle ───────────────────────────────────────────────────

    async def start(self, *, run_now: bool = False) -> None:
        if self._scheduler is not None:
            return

        if not settings.sync_enabled:
            logger.info("sync_enabled=false — scheduler not starting")
            return

        sched = AsyncIOScheduler(timezone="UTC")
        interval = max(1, settings.sync_interval_hours)
        for source, minute in _SOURCE_CRON_MINUTES.items():
            # APScheduler's hour field ranges 0..23, so `*/N` only works
            # for N <= 23. At a 24h+ cadence we pin hour=0 (daily UTC)
            # and stagger via minute slots, which is what the layout
            # already promises.
            if interval >= 24:
                trigger = CronTrigger(hour=0, minute=minute)
            else:
                trigger = CronTrigger(hour=f"*/{interval}", minute=minute)
            sched.add_job(
                self._job_run_source,
                trigger=trigger,
                args=[source],
                id=f"sync_{source}",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=600,
                replace_existing=True,
            )
            logger.info(
                "Scheduled %s sync — every %dh at minute %02d",
                source, interval, minute,
            )

        sched.start()
        self._scheduler = sched

        if run_now:
            asyncio.create_task(self._initial_kick())

    async def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    async def _initial_kick(self) -> None:
        """Run a fast first sync on startup so the UI isn't empty."""
        try:
            if self.workers["salesforce"].is_available():
                await self.sync_index_now()
            # Refresh global portal data first so per-account insights
            # syncs (which read the cache) see fresh advisories/EOL.
            asyncio.create_task(self.sync_source_now("portal_globals"))
            for source in _SOURCE_CRON_MINUTES:
                if source == "portal_globals":
                    continue
                if not self.workers[source].is_available():
                    continue
                asyncio.create_task(self.sync_source_now(source))
        except Exception:
            logger.exception("Initial sync kick failed")

    # ── Job entry points ────────────────────────────────────────────

    async def _job_run_source(self, source: str) -> None:
        """APScheduler entry — runs index (if SF) then per-account batch."""
        try:
            if source == "salesforce":
                await self.sync_index_now()
            await self.sync_source_now(source)
        except Exception:
            logger.exception("Cron job for %s failed", source)

    async def sync_index_now(self) -> int:
        """Refresh the SF account index. Returns row count written."""
        worker = self.workers["salesforce"]
        if not worker.is_available():
            return 0
        run_id = self.db.start_sync_run("salesforce", kind="index")
        try:
            count = await worker.sync_index()
            self.db.finish_sync_run(
                run_id, status="ok", accounts_processed=count,
                notes=f"account index refreshed ({count} rows)",
            )
            logger.info("Account index refreshed: %d rows", count)
            return count
        except Exception as exc:
            self.db.finish_sync_run(
                run_id, status="failed",
                errors=[{"error": str(exc), "type": type(exc).__name__}],
            )
            logger.exception("Account index refresh failed")
            return 0

    async def sync_source_now(self, source: str) -> dict[str, Any]:
        """
        Run the watch-list batch for one source, then recompute profiles
        and aggregates for the affected accounts.
        """
        if source == "portal_globals":
            return await self._sync_portal_globals_now()

        worker = self.workers.get(source)
        if worker is None:
            raise ValueError(f"unknown source: {source}")
        if not worker.is_available():
            logger.info("%s worker unavailable — skipping batch", source)
            return {"source": source, "skipped": True}

        async with self._source_locks[source]:
            run_id = self.db.start_sync_run(source, kind="portfolio")
            watch = await self._build_watch_list()
            cap = settings.sync_max_accounts_per_run
            if cap and cap > 0:
                watch = watch[:cap]

            sem = asyncio.Semaphore(max(1, settings.sync_account_concurrency))
            results: list[WorkerResult] = []
            ai_actions: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []

            async def _run_one(aid: str, name: str) -> None:
                async with sem:
                    res = await worker.sync_account(aid, name)
                    results.append(res)
                    for h in res.ai_actions:
                        ai_actions.append(h.to_dict())
                    if res.status == "failed":
                        errors.append({
                            "account_id": aid, "name": name,
                            "error": res.error,
                        })
                    elif res.payload:
                        await self._publish_snapshot(
                            source, aid, name, res.payload,
                        )

            await asyncio.gather(*[_run_one(a["id"], a["name"]) for a in watch])

            # Recompute risk profiles for accounts we just refreshed.
            for a in watch:
                await asyncio.to_thread(
                    self._recompute_one, a["id"], a["name"],
                )

            # Refresh the portfolio aggregate (cheap; small N at high risk).
            await asyncio.to_thread(self.aggregator_recompute.recompute_portfolio, "high")

            ok = sum(1 for r in results if r.status == "ok")
            fb = sum(1 for r in results if r.status == "fallback")
            failed = sum(1 for r in results if r.status == "failed")
            status = "ok" if failed == 0 else ("partial" if ok + fb > 0 else "failed")
            self.db.finish_sync_run(
                run_id, status=status,
                accounts_processed=len(results),
                errors=errors, ai_actions=ai_actions,
                notes=f"ok={ok} fallback={fb} failed={failed}",
            )
            logger.info(
                "%s batch complete — ok=%d fallback=%d failed=%d (%d AI actions)",
                source, ok, fb, failed, len(ai_actions),
            )
            return {
                "source": source, "ok": ok, "fallback": fb,
                "failed": failed, "total": len(results),
                "ai_actions": len(ai_actions),
            }

    async def _sync_portal_globals_now(self) -> dict[str, Any]:
        """Refresh the global portal cache and log a sync_runs row.

        Different return shape from per-account syncs because there's no
        watch list and no profile recompute — the result is just a summary
        of which blobs refreshed and from where.
        """
        async with self._source_locks["portal_globals"]:
            run_id = self.db.start_sync_run("portal_globals", kind="cache")
            try:
                result = await self.globals_worker.sync_now()
            except Exception as exc:
                self.db.finish_sync_run(
                    run_id, status="failed",
                    errors=[{"error": str(exc), "type": type(exc).__name__}],
                )
                logger.exception("portal_globals refresh crashed")
                return {"source": "portal_globals", "ok": False, "error": str(exc)}

            blobs = (result.payload or {}).get("blobs", []) if result.payload else []
            ok_count = sum(1 for b in blobs if b.get("ok"))
            notes = "; ".join(
                f"{b['key'].split(':', 1)[1]}={b['provenance']}"
                + (f"({b['detail']})" if b.get("detail") else "")
                for b in blobs
            )
            self.db.finish_sync_run(
                run_id, status=result.status,
                accounts_processed=ok_count,
                notes=notes or "no blobs",
            )
            return {
                "source": "portal_globals",
                "status": result.status,
                "blobs": blobs,
                "duration_ms": result.duration_ms,
            }

    async def sync_account_now(
        self, account_id: str, account_name: str,
    ) -> dict[str, Any]:
        """
        Refresh every source for a single account on demand. Used by the
        UI when a user opens an account whose snapshots are stale.
        """
        lock = self._account_locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            results: dict[str, WorkerResult] = {}
            for source, worker in self.workers.items():
                if not worker.is_available():
                    continue
                res = await worker.sync_account(account_id, account_name)
                results[source] = res
                if res.status != "failed" and res.payload:
                    await self._publish_snapshot(
                        source, account_id, account_name, res.payload,
                    )

            await asyncio.to_thread(
                self._recompute_one, account_id, account_name,
            )
            return {
                "account_id": account_id,
                "results": {
                    s: {"status": r.status, "error": r.error}
                    for s, r in results.items()
                },
            }

    # ── Internals ───────────────────────────────────────────────────

    async def _publish_snapshot(
        self,
        source: str,
        account_id: str,
        account_name: str,
        payload: dict[str, Any],
    ) -> None:
        """Fan a freshly-written snapshot out to the vector store and bus.

        Always runs the local ChromaDB upsert (it's a few-millisecond
        embedding lookup once the model is warm). When ``EVENT_BUS_ENABLED``
        is true, also publishes the snapshot envelope onto the bus so
        downstream consumers (alerting, replay-only re-indexing, …) can
        subscribe.
        """
        try:
            await asyncio.to_thread(
                self.vector_store.upsert_snapshot,
                source, account_id, account_name, payload,
            )
        except Exception:
            logger.debug(
                "vector_store.upsert_snapshot failed for %s/%s",
                source, account_id, exc_info=True,
            )

        bus = self.event_bus
        if bus is None or not bus.enabled:
            return
        try:
            await bus.publish(source, {
                "source": source,
                "account_id": account_id,
                "account_name": account_name,
                "fetched_at": payload.get("_fetched_at"),
                "snapshot": payload,
            })
        except Exception:
            logger.debug(
                "event_bus.publish failed for %s/%s",
                source, account_id, exc_info=True,
            )

    def _recompute_one(self, account_id: str, account_name: str) -> None:
        profile = self.recomputer.recompute(account_id, account_name)
        if profile:
            self.aggregator_recompute.recompute_account(account_id, profile)

    async def _build_watch_list(self) -> list[dict[str, str]]:
        """
        Compose the per-cycle watch list.

        Strategy is conservative: only refresh accounts we know are
        relevant. The first run will be small; additional accounts are
        added on-demand as the UI views them (via /api/sync/account).

        Every candidate is filtered against the ``accounts`` table —
        unless it appears there as a real Salesforce ID it never reaches
        a worker. This keeps stale UI-triggered profiles, demo IDs left
        over from a development run, and misspelt manual entries from
        polluting the live SOQL batch.
        """
        seen: dict[str, str] = {}

        # 1) Accounts with existing risk profiles
        for row in self.db.iter_risk_profiles():
            if row.account_id and row.account_name:
                seen[row.account_id] = row.account_name

        # 2) Accounts with active CXM custom risks
        try:
            for aid, items in self.custom_risks._data.items():  # type: ignore[attr-defined]
                if not items:
                    continue
                if aid in seen:
                    continue
                acct = self.db.get_account(aid)
                if acct:
                    seen[aid] = acct["name"]
        except Exception:
            logger.debug("CXM custom-risk enumeration skipped", exc_info=True)

        # 3) Accounts with escalated cases (Salesforce live query)
        sf = self.ctx.sf
        if sf is not None:
            try:
                escalated = await asyncio.to_thread(sf.get_escalated_cases, 90)
                for case in escalated:
                    aid = case.get("AccountId")
                    aname = (case.get("Account") or {}).get("Name", "")
                    if aid and aname and aid not in seen:
                        seen[aid] = aname
            except Exception:
                logger.warning("escalated-case query for watch list failed", exc_info=True)

        # ── Final guard: every entry must exist in the accounts table ──
        # Real Salesforce IDs are always present after a successful index
        # sync (26K+ rows). Anything else is either a stale demo entry
        # from earlier dev runs, an account that has been deleted/merged
        # in SFDC, or a typo from a manual on-demand call. Any of those
        # would crash the SOQL query with INVALID_QUERY_FILTER_OPERATOR.
        # We skip them silently (logging once for traceability) instead
        # of letting them inflate the failed-run count.
        if self.db.get_account_count() == 0:
            # Index hasn't been populated yet — skip the guard so the
            # first cycle can still bootstrap on-demand syncs.
            return [{"id": k, "name": v} for k, v in seen.items()]

        watch: list[dict[str, str]] = []
        skipped: list[str] = []
        for aid, name in seen.items():
            if self.db.get_account(aid) is not None:
                watch.append({"id": aid, "name": name})
            else:
                skipped.append(aid)

        if skipped:
            logger.info(
                "Watch list filter dropped %d ID(s) not present in accounts "
                "table (likely demo / stale): %s",
                len(skipped), ", ".join(skipped[:5]) + ("..." if len(skipped) > 5 else ""),
            )

        return watch


# ── Standalone CLI ──────────────────────────────────────────────────

async def _build_default_context() -> WorkerContext:
    """Construct a WorkerContext from settings for standalone runs."""
    sf = SalesforceConnector() if settings.sf_live and settings.sf_username else None

    bridge = None
    insights = None
    csinsights = None
    planhat = None
    if settings.insights_live or settings.csinsights_live or settings.planhat_live:
        try:
            from app.connectors.browser_bridge import PlaywrightBridge
            bridge = PlaywrightBridge()
            await bridge.start()
            if settings.insights_live and await bridge.ensure_portal_auth():
                insights = NutanixInsightsConnector(bridge)
            if settings.csinsights_live and await bridge.ensure_cs_auth():
                csinsights = CSInsightsConnector(bridge)
            if settings.planhat_live and await bridge.ensure_planhat_auth():
                planhat = PlanhatConnector(bridge)
        except Exception:
            logger.exception("Browser bridge init failed — portals disabled")

    glean = InternalEngagementConnector(sf) if sf and settings.engagement_live else None
    return WorkerContext(
        sf=sf, insights=insights, csinsights=csinsights, planhat=planhat,
        glean=glean, bridge=bridge, db=get_db(),
    )


async def _run_standalone(args: argparse.Namespace) -> None:
    ctx = await _build_default_context()
    sched = Scheduler(ctx)
    await sched.start(run_now=args.run_now)
    logger.info("Standalone scheduler running — Ctrl+C to stop")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await sched.shutdown()
    if ctx.bridge:
        try:
            await ctx.bridge.stop()
        except Exception:
            pass


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    )
    parser = argparse.ArgumentParser(description="Background ingestion scheduler")
    parser.add_argument(
        "--run-now", action="store_true",
        help="Trigger an initial sync immediately on startup",
    )
    args = parser.parse_args()
    asyncio.run(_run_standalone(args))


if __name__ == "__main__":
    main()
