"""
SQLite-backed durable store for the Adoption & Escalation Risk Analyzer.

All connector reads now flow through background ingestion workers (see
``app/sync/``) which write into the tables defined here. The FastAPI app
reads from these tables — the request path no longer hits any external
data source.

Schema (created lazily on first connect):

* ``accounts``         — full Salesforce account index, refreshed every sync
* ``risk_profiles``    — pre-computed ``AccountRiskProfile.to_dict()`` JSON
* ``source_snapshots`` — raw per-source payloads (salesforce, insights, …)
* ``sync_runs``        — audit log of every scheduler invocation
* ``risk_aggregates``  — pre-computed pie-chart slices per account / portfolio

WAL is enabled so the scheduler process can write while the app process
reads concurrently.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from app.config import settings

logger = logging.getLogger(__name__)


SCHEMA_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS accounts (
        id              TEXT PRIMARY KEY,
        name            TEXT NOT NULL,
        type            TEXT,
        industry        TEXT,
        owner           TEXT,
        cxm             TEXT,
        cxm_email       TEXT,
        cxm_program     TEXT,
        name_lower      TEXT,
        billing_country TEXT,
        region          TEXT,
        last_synced_at  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_accounts_name_lower ON accounts(name_lower)",
    "CREATE INDEX IF NOT EXISTS idx_accounts_cxm ON accounts(cxm)",
    # NOTE: idx_accounts_region is intentionally created by _run_migrations()
    # so it executes *after* the region column is back-filled on older DBs
    # (the CREATE TABLE here is a no-op when the table already exists).
    """
    CREATE TABLE IF NOT EXISTS source_snapshots (
        account_id      TEXT NOT NULL,
        source          TEXT NOT NULL,
        payload_json    TEXT NOT NULL,
        fetched_at      TEXT NOT NULL,
        source_version  TEXT,
        PRIMARY KEY (account_id, source)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_source_snapshots_source ON source_snapshots(source)",
    "CREATE INDEX IF NOT EXISTS idx_source_snapshots_fetched ON source_snapshots(fetched_at)",
    """
    CREATE TABLE IF NOT EXISTS risk_profiles (
        account_id      TEXT PRIMARY KEY,
        account_name    TEXT,
        profile_json    TEXT NOT NULL,
        overall_score   REAL,
        risk_level      TEXT,
        computed_at     TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_risk_profiles_score ON risk_profiles(overall_score DESC)",
    "CREATE INDEX IF NOT EXISTS idx_risk_profiles_level ON risk_profiles(risk_level)",
    """
    CREATE TABLE IF NOT EXISTS sync_runs (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        source              TEXT NOT NULL,
        kind                TEXT NOT NULL,        -- 'index' | 'account' | 'portfolio'
        started_at          TEXT NOT NULL,
        finished_at         TEXT,
        status              TEXT NOT NULL,         -- 'running' | 'ok' | 'partial' | 'failed'
        accounts_processed  INTEGER DEFAULT 0,
        errors_json         TEXT,
        ai_actions_json     TEXT,
        notes               TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sync_runs_source ON sync_runs(source, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sync_runs_started ON sync_runs(started_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS risk_aggregates (
        scope           TEXT NOT NULL,             -- 'account' | 'portfolio'
        scope_key       TEXT NOT NULL,             -- account_id, or 'all' / 'high' / etc
        slice_source    TEXT NOT NULL,             -- salesforce|insights|csinsights|glean|cxm
        weighted_total  REAL NOT NULL DEFAULT 0,
        account_count   INTEGER NOT NULL DEFAULT 0,
        top_factors     TEXT,                      -- JSON list of factor descriptions
        top_accounts    TEXT,                      -- JSON list (portfolio scope only)
        computed_at     TEXT NOT NULL,
        PRIMARY KEY (scope, scope_key, slice_source)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_risk_aggregates_scope ON risk_aggregates(scope, scope_key)",
    # Portal-wide reference data (security advisories, field advisories, EOL,
    # alerts) cached so per-account scoring still works when portal.example.com
    # is unauthenticated. ``provenance`` tracks how each blob was obtained:
    # ``unauth`` (public endpoint, fresh), ``bridge`` (authenticated SPA
    # fetch), or ``stale`` (last-good copy served past its expiry window).
    """
    CREATE TABLE IF NOT EXISTS portal_globals_cache (
        cache_key       TEXT PRIMARY KEY,
        payload_json    TEXT NOT NULL,
        provenance      TEXT NOT NULL,
        fetched_at      TEXT NOT NULL,
        expires_at      TEXT
    )
    """,
    # Salesforce → Planhat company-ID cache. Populated by
    # PlanhatConnector._resolve_company_id on first successful name match
    # so subsequent ticks don't re-search.
    """
    CREATE TABLE IF NOT EXISTS planhat_account_map (
        sf_account_id       TEXT PRIMARY KEY,
        planhat_company_id  TEXT NOT NULL,
        match_method        TEXT,
        matched_at          TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_planhat_map_phid ON planhat_account_map(planhat_company_id)",
]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RiskProfileRow:
    account_id: str
    account_name: str
    profile: dict[str, Any]
    overall_score: float
    risk_level: str
    computed_at: str

    def age_minutes(self) -> int:
        try:
            then = datetime.fromisoformat(self.computed_at)
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - then
            return max(0, int(delta.total_seconds() // 60))
        except (ValueError, TypeError):
            return -1


class Database:
    """
    Thread-safe SQLite store with WAL.

    One connection per thread is created lazily via ``thread_local``; the
    SQLite library is fully thread-safe in this mode. Writes serialise
    naturally because WAL admits a single writer at a time, but readers
    do not block.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path or settings.sync_db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._tls = threading.local()
        self._init_lock = threading.Lock()
        self._initialised = False
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._path),
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # autocommit; we manage txns explicitly
            timeout=30.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = self._connect()
            self._tls.conn = conn
        return conn

    def _initialise(self) -> None:
        with self._init_lock:
            if self._initialised:
                return
            conn = self._conn()
            for stmt in SCHEMA_STATEMENTS:
                conn.execute(stmt)
            self._run_migrations(conn)
            self._initialised = True
            logger.info("Database initialised at %s", self._path)

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """Idempotent column / index additions for older databases.

        ``CREATE TABLE IF NOT EXISTS`` is a no-op when the table already
        exists, so any column added to the schema after first launch has
        to be backfilled with ``ALTER TABLE`` — that's what this does.
        Each helper short-circuits when the change is already present.
        """
        self._add_column_if_missing(conn, "accounts", "billing_country", "TEXT")
        self._add_column_if_missing(conn, "accounts", "region", "TEXT")
        self._add_index_if_missing(
            conn, "idx_accounts_region", "accounts(region)",
        )

    @staticmethod
    def _add_column_if_missing(
        conn: sqlite3.Connection, table: str, column: str, decl: str,
    ) -> None:
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if column in existing:
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        logger.info("DB migration: added %s.%s %s", table, column, decl)

    @staticmethod
    def _add_index_if_missing(
        conn: sqlite3.Connection, index_name: str, on_clause: str,
    ) -> None:
        existing = {
            row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'",
            )
        }
        if index_name in existing:
            return
        conn.execute(f"CREATE INDEX {index_name} ON {on_clause}")
        logger.info("DB migration: created index %s ON %s", index_name, on_clause)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Explicit transaction; rolls back on exception."""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ── Accounts ─────────────────────────────────────────────────────

    def replace_account_index(self, accounts: list[dict[str, Any]]) -> int:
        """
        Atomically replace the entire account index. Used by the Salesforce
        index-sync worker on a successful pull of all customer accounts.
        """
        now = _utcnow()
        with self.transaction() as conn:
            conn.execute("DELETE FROM accounts")
            conn.executemany(
                """
                INSERT INTO accounts(
                    id, name, type, industry, owner, cxm, cxm_email,
                    cxm_program, name_lower, billing_country, region,
                    last_synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        a["id"], a["name"], a.get("type", ""),
                        a.get("industry", ""), a.get("owner", ""),
                        a.get("cxm", ""), a.get("cxm_email", ""),
                        a.get("cxm_program", ""),
                        a.get("name_lower") or a["name"].lower(),
                        a.get("billing_country", ""),
                        a.get("region", "Unknown"),
                        now,
                    )
                    for a in accounts
                ],
            )
        return len(accounts)

    def get_account_count(self, region: Optional[str] = None) -> int:
        if region and region != "all":
            row = self._conn().execute(
                "SELECT COUNT(*) AS c FROM accounts WHERE region = ?",
                (region,),
            ).fetchone()
        else:
            row = self._conn().execute(
                "SELECT COUNT(*) AS c FROM accounts",
            ).fetchone()
        return int(row["c"]) if row else 0

    def list_accounts(
        self,
        offset: int = 0,
        limit: int = 100,
        region: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if region and region != "all":
            rows = self._conn().execute(
                "SELECT * FROM accounts WHERE region = ? "
                "ORDER BY name LIMIT ? OFFSET ?",
                (region, limit, offset),
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM accounts ORDER BY name LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]

    def search_accounts(
        self,
        query: str,
        limit: int = 50,
        region: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if not query or len(query) < 2:
            return []
        q_lower = query.lower()
        tokens = q_lower.split()
        # SQLite LIKE is case-insensitive by default for ASCII; for multi-token
        # search we filter in Python after pulling a coarse pre-filter.
        like = f"%{tokens[0]}%"
        if region and region != "all":
            rows = self._conn().execute(
                "SELECT * FROM accounts WHERE name_lower LIKE ? AND region = ? "
                "ORDER BY name LIMIT ?",
                (like, region, limit * 4),
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM accounts WHERE name_lower LIKE ? ORDER BY name LIMIT ?",
                (like, limit * 4),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for r in rows:
            name_l = r["name_lower"] or ""
            if all(tok in name_l for tok in tokens):
                results.append(dict(r))
                if len(results) >= limit:
                    break
        return results

    def get_account(self, account_id: str) -> Optional[dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_accounts_by_cxm(
        self,
        cxm_name: str,
        offset: int = 0,
        limit: int = 100,
        region: Optional[str] = None,
    ) -> tuple[list[dict[str, Any]], int]:
        like = f"%{cxm_name.lower()}%"
        if region and region != "all":
            total_row = self._conn().execute(
                "SELECT COUNT(*) AS c FROM accounts "
                "WHERE LOWER(cxm) LIKE ? AND region = ?",
                (like, region),
            ).fetchone()
            total = int(total_row["c"]) if total_row else 0
            rows = self._conn().execute(
                "SELECT * FROM accounts WHERE LOWER(cxm) LIKE ? AND region = ? "
                "ORDER BY name LIMIT ? OFFSET ?",
                (like, region, limit, offset),
            ).fetchall()
            return [dict(r) for r in rows], total
        total_row = self._conn().execute(
            "SELECT COUNT(*) AS c FROM accounts WHERE LOWER(cxm) LIKE ?",
            (like,),
        ).fetchone()
        total = int(total_row["c"]) if total_row else 0
        rows = self._conn().execute(
            "SELECT * FROM accounts WHERE LOWER(cxm) LIKE ? ORDER BY name LIMIT ? OFFSET ?",
            (like, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows], total

    def cxm_roster(self, region: Optional[str] = None) -> list[dict[str, Any]]:
        if region and region != "all":
            rows = self._conn().execute(
                """
                SELECT cxm AS name, cxm_email AS email, COUNT(*) AS account_count
                FROM accounts
                WHERE cxm IS NOT NULL AND cxm <> '' AND region = ?
                GROUP BY cxm, cxm_email
                ORDER BY cxm
                """,
                (region,),
            ).fetchall()
        else:
            rows = self._conn().execute(
                """
                SELECT cxm AS name, cxm_email AS email, COUNT(*) AS account_count
                FROM accounts
                WHERE cxm IS NOT NULL AND cxm <> ''
                GROUP BY cxm, cxm_email
                ORDER BY cxm
                """,
            ).fetchall()
        return [dict(r) for r in rows]

    def region_counts(self) -> dict[str, int]:
        """Return a count of accounts per region (for the UI badge counts)."""
        rows = self._conn().execute(
            """
            SELECT COALESCE(NULLIF(region, ''), 'Unknown') AS region,
                   COUNT(*) AS c
            FROM accounts
            GROUP BY COALESCE(NULLIF(region, ''), 'Unknown')
            """,
        ).fetchall()
        return {r["region"]: int(r["c"]) for r in rows}

    # ── Source snapshots ─────────────────────────────────────────────

    def upsert_source_snapshot(
        self,
        account_id: str,
        source: str,
        payload: dict[str, Any],
        source_version: str = "",
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO source_snapshots(
                    account_id, source, payload_json, fetched_at, source_version
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_id, source) DO UPDATE SET
                    payload_json   = excluded.payload_json,
                    fetched_at     = excluded.fetched_at,
                    source_version = excluded.source_version
                """,
                (account_id, source, json.dumps(payload), _utcnow(), source_version),
            )

    def get_source_snapshot(
        self, account_id: str, source: str,
    ) -> Optional[dict[str, Any]]:
        row = self._conn().execute(
            """
            SELECT payload_json, fetched_at FROM source_snapshots
            WHERE account_id = ? AND source = ?
            """,
            (account_id, source),
        ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            return None
        payload["_fetched_at"] = row["fetched_at"]
        return payload

    def get_all_source_snapshots(
        self, account_id: str,
    ) -> dict[str, dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT source, payload_json, fetched_at FROM source_snapshots WHERE account_id = ?",
            (account_id,),
        ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            try:
                payload = json.loads(r["payload_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            payload["_fetched_at"] = r["fetched_at"]
            out[r["source"]] = payload
        return out

    def source_freshness(self) -> dict[str, str]:
        """Most-recent fetched_at per source — used for staleness badges."""
        rows = self._conn().execute(
            "SELECT source, MAX(fetched_at) AS latest FROM source_snapshots GROUP BY source",
        ).fetchall()
        return {r["source"]: r["latest"] for r in rows if r["latest"]}

    # ── Risk profiles ─────────────────────────────────────────────────

    def upsert_risk_profile(
        self,
        account_id: str,
        account_name: str,
        profile: dict[str, Any],
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO risk_profiles(
                    account_id, account_name, profile_json,
                    overall_score, risk_level, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    account_name  = excluded.account_name,
                    profile_json  = excluded.profile_json,
                    overall_score = excluded.overall_score,
                    risk_level    = excluded.risk_level,
                    computed_at   = excluded.computed_at
                """,
                (
                    account_id, account_name, json.dumps(profile),
                    float(profile.get("overall_score") or 0),
                    profile.get("risk_level") or "low",
                    _utcnow(),
                ),
            )

    def get_risk_profile(self, account_id: str) -> Optional[RiskProfileRow]:
        row = self._conn().execute(
            "SELECT * FROM risk_profiles WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if not row:
            return None
        try:
            profile = json.loads(row["profile_json"])
        except (json.JSONDecodeError, TypeError):
            return None
        return RiskProfileRow(
            account_id=row["account_id"],
            account_name=row["account_name"] or "",
            profile=profile,
            overall_score=float(row["overall_score"] or 0),
            risk_level=row["risk_level"] or "low",
            computed_at=row["computed_at"],
        )

    def top_risk_profiles(
        self,
        limit: int = 20,
        min_score: float = 0,
        region: Optional[str] = None,
    ) -> list[RiskProfileRow]:
        if region and region != "all":
            rows = self._conn().execute(
                """
                SELECT rp.* FROM risk_profiles rp
                JOIN accounts a ON a.id = rp.account_id
                WHERE rp.overall_score >= ? AND a.region = ?
                ORDER BY rp.overall_score DESC LIMIT ?
                """,
                (min_score, region, limit),
            ).fetchall()
        else:
            rows = self._conn().execute(
                """
                SELECT * FROM risk_profiles
                WHERE overall_score >= ?
                ORDER BY overall_score DESC LIMIT ?
                """,
                (min_score, limit),
            ).fetchall()
        out: list[RiskProfileRow] = []
        for r in rows:
            try:
                profile = json.loads(r["profile_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            out.append(RiskProfileRow(
                account_id=r["account_id"],
                account_name=r["account_name"] or "",
                profile=profile,
                overall_score=float(r["overall_score"] or 0),
                risk_level=r["risk_level"] or "low",
                computed_at=r["computed_at"],
            ))
        return out

    def iter_risk_profiles(self) -> Iterator[RiskProfileRow]:
        """Stream every profile — used by aggregate recomputation."""
        cursor = self._conn().execute("SELECT * FROM risk_profiles")
        for r in cursor:
            try:
                profile = json.loads(r["profile_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            yield RiskProfileRow(
                account_id=r["account_id"],
                account_name=r["account_name"] or "",
                profile=profile,
                overall_score=float(r["overall_score"] or 0),
                risk_level=r["risk_level"] or "low",
                computed_at=r["computed_at"],
            )

    def profile_count(self) -> int:
        row = self._conn().execute("SELECT COUNT(*) AS c FROM risk_profiles").fetchone()
        return int(row["c"]) if row else 0

    # ── Sync runs (audit log) ────────────────────────────────────────

    def start_sync_run(self, source: str, kind: str = "portfolio") -> int:
        cursor = self._conn().execute(
            """
            INSERT INTO sync_runs(source, kind, started_at, status)
            VALUES (?, ?, ?, 'running')
            """,
            (source, kind, _utcnow()),
        )
        return int(cursor.lastrowid)

    def finish_sync_run(
        self,
        run_id: int,
        status: str,
        accounts_processed: int = 0,
        errors: Optional[list[dict[str, Any]]] = None,
        ai_actions: Optional[list[dict[str, Any]]] = None,
        notes: str = "",
    ) -> None:
        self._conn().execute(
            """
            UPDATE sync_runs
            SET finished_at = ?, status = ?, accounts_processed = ?,
                errors_json = ?, ai_actions_json = ?, notes = ?
            WHERE id = ?
            """,
            (
                _utcnow(), status, accounts_processed,
                json.dumps(errors or []), json.dumps(ai_actions or []),
                notes, run_id,
            ),
        )

    def list_sync_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            for fld in ("errors_json", "ai_actions_json"):
                try:
                    d[fld[:-5]] = json.loads(d.pop(fld) or "[]")
                except (json.JSONDecodeError, TypeError):
                    d[fld[:-5]] = []
            out.append(d)
        return out

    def latest_run_per_source(self) -> dict[str, dict[str, Any]]:
        rows = self._conn().execute(
            """
            SELECT s.* FROM sync_runs s
            INNER JOIN (
                SELECT source, MAX(started_at) AS latest
                FROM sync_runs WHERE status IN ('ok', 'partial')
                GROUP BY source
            ) m ON s.source = m.source AND s.started_at = m.latest
            """,
        ).fetchall()
        return {r["source"]: dict(r) for r in rows}

    # ── Risk aggregates (pie chart cache) ────────────────────────────

    def replace_aggregates(
        self, scope: str, scope_key: str, slices: list[dict[str, Any]],
    ) -> None:
        now = _utcnow()
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM risk_aggregates WHERE scope = ? AND scope_key = ?",
                (scope, scope_key),
            )
            conn.executemany(
                """
                INSERT INTO risk_aggregates(
                    scope, scope_key, slice_source, weighted_total,
                    account_count, top_factors, top_accounts, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        scope, scope_key, s["source"],
                        float(s.get("weighted_total") or 0),
                        int(s.get("account_count") or 0),
                        json.dumps(s.get("top_factors") or []),
                        json.dumps(s.get("top_accounts") or []),
                        now,
                    )
                    for s in slices
                ],
            )

    # ── Portal globals cache (advisories, EOL, alerts) ───────────────

    def upsert_global_cache(
        self,
        cache_key: str,
        payload: Any,
        provenance: str,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        """Replace one global-cache blob.

        ``provenance`` is one of ``unauth`` | ``bridge`` | ``stale``. The
        ``ttl_seconds`` is advisory only — readers decide what counts as
        stale. Storing it here lets us serve the same blob with provenance
        flipped to ``stale`` once the window has passed.
        """
        now_dt = datetime.now(timezone.utc)
        expires_iso: Optional[str] = None
        if ttl_seconds is not None and ttl_seconds > 0:
            expires_iso = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO portal_globals_cache(
                    cache_key, payload_json, provenance, fetched_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    provenance   = excluded.provenance,
                    fetched_at   = excluded.fetched_at,
                    expires_at   = excluded.expires_at
                """,
                (cache_key, json.dumps(payload), provenance,
                 now_dt.isoformat(), expires_iso),
            )

    def get_global_cache(self, cache_key: str) -> Optional[dict[str, Any]]:
        """Return the cached blob plus metadata, or ``None`` if missing.

        Output shape: ``{"payload": ..., "provenance": str, "fetched_at": iso,
        "expires_at": iso|None, "is_stale": bool}``. ``is_stale`` is a
        convenience flag: ``True`` when ``expires_at`` exists and lies in
        the past.
        """
        row = self._conn().execute(
            """
            SELECT payload_json, provenance, fetched_at, expires_at
            FROM portal_globals_cache WHERE cache_key = ?
            """,
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            return None
        is_stale = False
        expires = row["expires_at"]
        if expires:
            try:
                exp_dt = datetime.fromisoformat(expires)
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                is_stale = datetime.now(timezone.utc) > exp_dt
            except (ValueError, TypeError):
                pass
        return {
            "payload": payload,
            "provenance": row["provenance"],
            "fetched_at": row["fetched_at"],
            "expires_at": expires,
            "is_stale": is_stale,
        }

    def list_global_caches(self) -> list[dict[str, Any]]:
        """Metadata for every cached blob — used by the sync UI for badges."""
        rows = self._conn().execute(
            """
            SELECT cache_key, provenance, fetched_at, expires_at,
                   length(payload_json) AS bytes
            FROM portal_globals_cache ORDER BY cache_key
            """,
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Planhat account mapping ──────────────────────────────────────

    def get_planhat_id(self, sf_account_id: str) -> Optional[str]:
        """Return the cached Planhat company ID for ``sf_account_id``."""
        row = self._conn().execute(
            "SELECT planhat_company_id FROM planhat_account_map WHERE sf_account_id = ?",
            (sf_account_id,),
        ).fetchone()
        return row["planhat_company_id"] if row else None

    def set_planhat_id(
        self,
        sf_account_id: str,
        planhat_company_id: str,
        match_method: str = "name_exact",
    ) -> None:
        """Persist a Salesforce → Planhat mapping."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO planhat_account_map(
                    sf_account_id, planhat_company_id, match_method, matched_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(sf_account_id) DO UPDATE SET
                    planhat_company_id = excluded.planhat_company_id,
                    match_method       = excluded.match_method,
                    matched_at         = excluded.matched_at
                """,
                (sf_account_id, planhat_company_id, match_method, _utcnow()),
            )

    def list_planhat_mappings(self) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM planhat_account_map ORDER BY matched_at DESC",
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Risk aggregates queries ──────────────────────────────────────

    def get_aggregates(
        self, scope: str, scope_key: str,
    ) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            """
            SELECT * FROM risk_aggregates
            WHERE scope = ? AND scope_key = ?
            ORDER BY weighted_total DESC
            """,
            (scope, scope_key),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            for fld in ("top_factors", "top_accounts"):
                try:
                    d[fld] = json.loads(d.get(fld) or "[]")
                except (json.JSONDecodeError, TypeError):
                    d[fld] = []
            out.append(d)
        return out


# ── Module-level singleton ─────────────────────────────────────────

_db_singleton: Optional[Database] = None
_singleton_lock = threading.Lock()


def get_db() -> Database:
    global _db_singleton
    if _db_singleton is None:
        with _singleton_lock:
            if _db_singleton is None:
                _db_singleton = Database()
    return _db_singleton
