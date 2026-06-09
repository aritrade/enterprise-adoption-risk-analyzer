"""
Base contract for ingestion workers.

Workers are stateful: they hold references to live connector instances
(Salesforce, Playwright bridge, …) but persist *all* output through the
``Database`` interface in :mod:`app.store.db`. They never block the
FastAPI request path — only the scheduler in :mod:`app.sync.scheduler`
invokes them.

Each worker implements :meth:`sync_account` and (optionally)
:meth:`sync_index`. Errors flow through :meth:`run_with_heal` which:

1. Catches the exception.
2. Builds a :class:`FailureContext` and asks ``ai_heal.diagnose_failure``
   for a verdict.
3. Honours the verdict with a bounded retry / fallback strategy.
4. Returns a :class:`WorkerResult` describing what happened and the AI
   actions taken — the scheduler logs all of this into ``sync_runs``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from app.config import settings
from app.connectors.csinsights import CSInsightsConnector
from app.connectors.glean import InternalEngagementConnector
from app.connectors.nutanix_insights import NutanixInsightsConnector
from app.connectors.planhat import PlanhatConnector
from app.connectors.salesforce import SalesforceConnector
from app.store.db import Database, get_db
from app.sync.ai_heal import FailureContext, HealAction, diagnose_failure

logger = logging.getLogger(__name__)


# ── Shared connector context ─────────────────────────────────────────

@dataclass
class WorkerContext:
    """Container for live connector handles passed to every worker."""
    sf: Optional[SalesforceConnector] = None
    insights: Optional[NutanixInsightsConnector] = None
    csinsights: Optional[CSInsightsConnector] = None
    planhat: Optional[PlanhatConnector] = None
    glean: Optional[InternalEngagementConnector] = None
    bridge: Any = None  # PlaywrightBridge — kept generic to avoid cyclic import
    db: Database = field(default_factory=get_db)


# ── Result types ─────────────────────────────────────────────────────

@dataclass
class WorkerResult:
    """Outcome of one ``sync_account`` (or ``sync_index``) call."""
    source: str
    account_id: str = ""
    status: str = "ok"           # 'ok' | 'fallback' | 'failed'
    payload: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    duration_ms: int = 0
    ai_actions: list[HealAction] = field(default_factory=list)


# ── Base worker ──────────────────────────────────────────────────────

class BaseWorker:
    """
    Subclasses must define ``source`` and implement ``_sync_account_impl``.

    The base provides :meth:`sync_account`, which wraps the impl with
    retry-with-heal and DB persistence.
    """

    source: str = ""
    expected_schema: str = "dict with non-empty fields"

    def __init__(self, ctx: WorkerContext) -> None:
        if not self.source:
            raise ValueError(f"{type(self).__name__} must define `source`")
        self.ctx = ctx

    # ── Hooks subclasses override ────────────────────────────────────

    async def _sync_account_impl(
        self, account_id: str, account_name: str,
    ) -> dict[str, Any]:
        """Pull the source snapshot for one account. Raise on failure."""
        raise NotImplementedError

    async def sync_index(self) -> int:
        """
        Refresh the lightweight index (account list, etc.). Default
        implementation is a no-op — only the Salesforce worker overrides
        this.
        """
        return 0

    def is_available(self) -> bool:
        """Whether this worker has the dependencies it needs to run."""
        return True

    def fallback_payload(
        self, account_id: str, account_name: str,
    ) -> Optional[dict[str, Any]]:
        """
        Return a deterministic fallback payload when both the live fetch
        and the AI heal layer have given up. ``None`` means "do not
        write a snapshot" — the existing one (if any) is preserved.
        """
        return None

    # ── Public entry point with retry + heal ─────────────────────────

    async def sync_account(
        self, account_id: str, account_name: str,
    ) -> WorkerResult:
        started = time.time()
        result = WorkerResult(source=self.source, account_id=account_id)

        if not self.is_available():
            result.status = "failed"
            result.error = f"{self.source} worker unavailable (missing connector or auth)"
            result.duration_ms = int((time.time() - started) * 1000)
            return result

        max_attempts = max(1, settings.ai_heal_max_retries)
        last_exc: Optional[BaseException] = None
        last_response_excerpt = ""

        for attempt in range(1, max_attempts + 1):
            try:
                payload = await self._sync_account_impl(account_id, account_name)
                if not payload or not isinstance(payload, dict):
                    raise ValueError(
                        f"{self.source} returned empty/invalid payload "
                        f"(type={type(payload).__name__})"
                    )
                self.ctx.db.upsert_source_snapshot(
                    account_id=account_id,
                    source=self.source,
                    payload=payload,
                    source_version=str(int(time.time())),
                )
                result.status = "ok"
                result.payload = payload
                result.duration_ms = int((time.time() - started) * 1000)
                return result

            except Exception as exc:
                last_exc = exc
                last_response_excerpt = self._exception_excerpt(exc)
                logger.warning(
                    "%s worker attempt %d/%d failed for %s: %s",
                    self.source, attempt, max_attempts, account_id, exc,
                )

                heal = await asyncio.to_thread(
                    diagnose_failure,
                    FailureContext(
                        source=self.source,
                        kind="account",
                        account_id=account_id,
                        expected_schema=self.expected_schema,
                        exception=exc,
                        response_excerpt=last_response_excerpt,
                        recent_failures=attempt - 1,
                    ),
                )
                result.ai_actions.append(heal)

                if heal.verdict == "transient_retry" and attempt < max_attempts:
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                if heal.verdict == "rate_limited":
                    await asyncio.sleep(min(2 ** (attempt + 1), 60))
                    if attempt < max_attempts:
                        continue
                # auth_expired / schema_drift / upstream_down / abort →
                # break out and fall back below
                break

        # All attempts exhausted — try fallback payload
        fallback = self.fallback_payload(account_id, account_name)
        if fallback:
            self.ctx.db.upsert_source_snapshot(
                account_id=account_id,
                source=self.source,
                payload=fallback,
                source_version="fallback",
            )
            result.status = "fallback"
            result.payload = fallback
            result.error = f"used fallback after {last_exc}"
            result.duration_ms = int((time.time() - started) * 1000)
            return result

        result.status = "failed"
        result.error = str(last_exc) if last_exc else "unknown error"
        result.duration_ms = int((time.time() - started) * 1000)
        return result

    @staticmethod
    def _exception_excerpt(exc: BaseException, limit: int = 1500) -> str:
        msg = str(exc)
        if len(msg) <= limit:
            return msg
        return msg[: limit // 2] + " ...[truncated]... " + msg[-limit // 2 :]
