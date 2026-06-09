"""
ChromaDB-backed vector store for connector snapshots.

The SQLite ``source_snapshots`` table is the durable source of truth.
This module mirrors every snapshot into a persistent ChromaDB instance
under ``data/chromadb/`` so the API can answer semantic search and
"similar accounts" queries without hammering the source connectors.

Layout:
    data/chromadb/
        snapshots_salesforce/   collection
        snapshots_insights/     collection
        snapshots_csinsights/   collection
        snapshots_planhat/      collection
        snapshots_glean/        collection
        snapshots_all/          collection (cross-source aggregate)

Each document id is ``f"{source}:{account_id}"`` so upserts are
idempotent. The text is a deterministic, field-aware rendering of the
snapshot — NOT the raw JSON — so the embedding model sees natural
language ("health score 72, declining; renewal in 47 days") instead of
escaped quotes and braces.

The module is import-safe even when ``chromadb`` /
``sentence-transformers`` are not installed; the wrapper degrades to a
no-op so the app starts cleanly in environments where the heavy ML deps
haven't been provisioned yet (CI, lightweight demo bundles, etc.).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


_DEFAULT_PATH = Path("data/chromadb")
_DEFAULT_EMBED_MODEL = os.environ.get(
    "VECTOR_STORE_EMBED_MODEL", "all-MiniLM-L6-v2",
)

_SOURCES = ("salesforce", "insights", "csinsights", "planhat", "glean")


# ──────────────────────────────────────────────────────────────────────
# Snapshot → text
# ──────────────────────────────────────────────────────────────────────

def _fmt_kv(label: str, value: Any) -> Optional[str]:
    """Compact ``label: value`` pair, or None when value is empty."""
    if value is None or value == "" or value == [] or value == {}:
        return None
    if isinstance(value, bool):
        return f"{label}: {'yes' if value else 'no'}"
    if isinstance(value, (int, float)):
        return f"{label}: {value}"
    if isinstance(value, str):
        return f"{label}: {value.strip()}"
    return None


def _join(parts: Iterable[Optional[str]]) -> str:
    return ". ".join(p for p in parts if p)


def _planhat_text(account_name: str, payload: dict[str, Any]) -> str:
    open_tasks = payload.get("open_tasks") or []
    task_titles = ", ".join(
        t.get("title", "") for t in open_tasks if isinstance(t, dict) and t.get("title")
    )
    open_convos = payload.get("open_conversations") or []
    convo_subjects = ", ".join(
        c.get("subject", "") for c in open_convos if isinstance(c, dict) and c.get("subject")
    )
    parts = [
        f"Account {account_name} (Planhat customer-success snapshot)",
        _fmt_kv("lifecycle phase", payload.get("lifecycle_phase")),
        _fmt_kv("health score", payload.get("health_score")),
        _fmt_kv("health trend", payload.get("health_trend")),
        _fmt_kv("NPS score", payload.get("nps_score")),
        _fmt_kv("NPS responded at", payload.get("nps_responded_at")),
        _fmt_kv("renewal date", payload.get("renewal_date")),
        _fmt_kv("days to renewal", payload.get("days_to_renewal")),
        _fmt_kv("ARR", payload.get("arr")),
        _fmt_kv("MRR", payload.get("mrr")),
        _fmt_kv("churn risk flag", payload.get("churn_risk_flag")),
        _fmt_kv("last meeting at", payload.get("last_meeting_at")),
        _fmt_kv("last email at", payload.get("last_email_at")),
        _fmt_kv("last touch at", payload.get("last_touch_at")),
        _fmt_kv("open tasks", task_titles),
        _fmt_kv("open conversations", convo_subjects),
    ]
    return _join(parts)


def _salesforce_text(account_name: str, payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    renewal = payload.get("renewal") or {}
    financials = payload.get("financials") or {}
    owner = payload.get("owner") or {}
    case_subjects: list[str] = []
    for c in (summary.get("cases") or [])[:10]:
        if isinstance(c, dict) and c.get("Subject"):
            case_subjects.append(str(c["Subject"]))
    parts = [
        f"Account {account_name} (Salesforce snapshot)",
        _fmt_kv("total cases", summary.get("total_cases")),
        _fmt_kv("open cases", summary.get("open_cases")),
        _fmt_kv("P1 cases", summary.get("p1_cases")),
        _fmt_kv("P2 cases", summary.get("p2_cases")),
        _fmt_kv("escalated cases", summary.get("escalated_cases")),
        _fmt_kv("renewal date", renewal.get("renewal_date")),
        _fmt_kv("days to renewal", renewal.get("days_to_renewal")),
        _fmt_kv("renewal risk", renewal.get("renewal_risk_label")),
        _fmt_kv("contract value", renewal.get("contract_value")),
        _fmt_kv("account owner", owner.get("name") or owner.get("Name")),
        _fmt_kv("account financials", financials.get("currentRevenue")),
        _fmt_kv("recent case subjects", "; ".join(case_subjects)),
    ]
    return _join(parts)


def _insights_text(account_name: str, payload: dict[str, Any]) -> str:
    hv = payload.get("hypervisor_distribution") or {}
    hw = payload.get("hw_partner_distribution") or {}
    top_hw = max(hw.items(), key=lambda x: x[1])[0] if hw else None
    parts = [
        f"Account {account_name} (Nutanix Insights snapshot)",
        _fmt_kv("total clusters", payload.get("total_clusters")),
        _fmt_kv("total nodes", payload.get("total_nodes")),
        _fmt_kv("critical alerts", payload.get("total_critical_alerts")),
        _fmt_kv("EOL exposure count", payload.get("eol_exposure_count")),
        _fmt_kv("contract status", payload.get("contract_status")),
        _fmt_kv("pulse enabled", payload.get("pulse_enabled")),
        _fmt_kv("AHV nodes", hv.get("AHV")),
        _fmt_kv("ESXi nodes", hv.get("ESXI") or hv.get("ESXi")),
        _fmt_kv("primary HW partner", top_hw),
    ]
    return _join(parts)


def _csinsights_text(account_name: str, payload: dict[str, Any]) -> str:
    meta = payload.get("account_metadata") or {}
    parts = [
        f"Account {account_name} (CS Insights snapshot)",
        _fmt_kv("CS health score", payload.get("health_score")),
        _fmt_kv("engagement score", payload.get("engagement_score")),
        _fmt_kv("renewal risk", payload.get("renewal_risk")),
        _fmt_kv("renewal date", payload.get("renewal_date")),
        _fmt_kv("days to renewal", payload.get("days_to_renewal")),
        _fmt_kv("contract value", payload.get("contract_value")),
        _fmt_kv("account owner", meta.get("account_owner")),
        _fmt_kv("systems engineer", meta.get("systems_engineer")),
        _fmt_kv("region", meta.get("region")),
        _fmt_kv("theater", meta.get("theater")),
        _fmt_kv("vertical", meta.get("vertical")),
        _fmt_kv("most recent sale", meta.get("most_recent_sale")),
    ]
    return _join(parts)


def _glean_text(account_name: str, payload: dict[str, Any]) -> str:
    parts = [
        f"Account {account_name} (Internal engagement / Glean snapshot)",
        _fmt_kv("total mentions", payload.get("total_mentions")),
        _fmt_kv("recent mentions (30d)", payload.get("recent_mentions_30d")),
        _fmt_kv("escalation mentions", payload.get("escalation_mentions")),
        _fmt_kv("last activity", payload.get("last_activity_date")),
    ]
    return _join(parts)


_TEXTUALIZERS = {
    "planhat":    _planhat_text,
    "salesforce": _salesforce_text,
    "insights":   _insights_text,
    "csinsights": _csinsights_text,
    "glean":      _glean_text,
}


def render_snapshot(source: str, account_name: str, payload: dict[str, Any]) -> str:
    """Public entry — render any source's snapshot to embedding-friendly text."""
    fn = _TEXTUALIZERS.get(source)
    if fn:
        return fn(account_name or "", payload or {})
    # Generic fallback: stringify scalar fields.
    parts: list[Optional[str]] = [f"Account {account_name} ({source} snapshot)"]
    for k, v in (payload or {}).items():
        if k.startswith("_") or k in ("raw",):
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            parts.append(_fmt_kv(k.replace("_", " "), v))
    return _join(parts)


# ──────────────────────────────────────────────────────────────────────
# Metadata builder
# ──────────────────────────────────────────────────────────────────────

def _scalar_metadata(
    source: str,
    account_id: str,
    account_name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Return ChromaDB-safe metadata (only str / int / float / bool)."""
    meta: dict[str, Any] = {
        "source": source,
        "account_id": account_id,
        "account_name": account_name or "",
        "fetched_at": payload.get("_fetched_at") or "",
    }
    if source == "planhat":
        for key in ("health_score", "nps_score", "lifecycle_phase",
                    "health_trend", "renewal_date", "churn_risk_flag",
                    "match_method"):
            v = payload.get(key)
            if isinstance(v, (str, int, float, bool)):
                meta[key] = v
    elif source == "csinsights":
        for key in ("health_score", "engagement_score", "renewal_risk",
                    "renewal_date"):
            v = payload.get(key)
            if isinstance(v, (str, int, float, bool)):
                meta[key] = v
        am = payload.get("account_metadata") or {}
        for key in ("region", "theater", "vertical"):
            v = am.get(key)
            if isinstance(v, str) and v:
                meta[key] = v
    elif source == "salesforce":
        summary = payload.get("summary") or {}
        for key in ("open_cases", "p1_cases", "p2_cases", "escalated_cases"):
            v = summary.get(key)
            if isinstance(v, (int, float)):
                meta[key] = v
    elif source == "insights":
        for key in ("total_clusters", "total_nodes",
                    "total_critical_alerts", "eol_exposure_count",
                    "contract_status", "pulse_enabled"):
            v = payload.get(key)
            if isinstance(v, (str, int, float, bool)):
                meta[key] = v
    return meta


# ──────────────────────────────────────────────────────────────────────
# Vector store wrapper (lazy chromadb import + thread-safe singleton)
# ──────────────────────────────────────────────────────────────────────

class VectorStore:
    """Thin wrapper around a persistent ChromaDB client.

    Soft-fails when the chromadb dependency is not installed: every
    method becomes a no-op and ``available`` returns False, so callers
    can keep working without crashing.
    """

    def __init__(
        self,
        persist_directory: Path = _DEFAULT_PATH,
        embed_model: str = _DEFAULT_EMBED_MODEL,
    ) -> None:
        self.persist_directory = Path(persist_directory)
        self.embed_model = embed_model
        self._client: Any = None
        self._embedder: Any = None
        self._collections: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._available: Optional[bool] = None

    @property
    def available(self) -> bool:
        if self._available is None:
            self._initialise()
        return bool(self._available)

    def _initialise(self) -> None:
        with self._lock:
            if self._available is not None:
                return
            try:
                import chromadb  # type: ignore
                from chromadb.utils import embedding_functions  # type: ignore
            except Exception as exc:
                logger.warning(
                    "chromadb not available; vector store disabled (%s)", exc,
                )
                self._available = False
                return

            try:
                self.persist_directory.mkdir(parents=True, exist_ok=True)
                self._client = chromadb.PersistentClient(
                    path=str(self.persist_directory),
                )
                self._embedder = embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name=self.embed_model,
                )
            except Exception:
                logger.exception("Vector store initialisation failed")
                self._available = False
                return

            self._available = True
            logger.info(
                "Vector store ready at %s (model=%s)",
                self.persist_directory, self.embed_model,
            )

    def _collection(self, name: str) -> Any:
        if not self.available:
            return None
        col = self._collections.get(name)
        if col is not None:
            return col
        with self._lock:
            col = self._collections.get(name)
            if col is None:
                col = self._client.get_or_create_collection(
                    name=name, embedding_function=self._embedder,
                )
                self._collections[name] = col
        return col

    @staticmethod
    def _doc_id(source: str, account_id: str) -> str:
        return f"{source}:{account_id}"

    @staticmethod
    def _collection_name(source: str) -> str:
        return f"snapshots_{source}"

    # ── Public API ──────────────────────────────────────────────────

    def upsert_snapshot(
        self,
        source: str,
        account_id: str,
        account_name: str,
        payload: Optional[dict[str, Any]],
    ) -> bool:
        """Upsert one snapshot into the per-source and unified collections.

        Returns True on success, False on any failure (logged at debug
        level — the caller treats this as best-effort).
        """
        if not self.available or not payload:
            return False
        text = render_snapshot(source, account_name, payload)
        if not text:
            return False
        meta = _scalar_metadata(source, account_id, account_name, payload)
        doc_id = self._doc_id(source, account_id)
        try:
            for col_name in (self._collection_name(source), "snapshots_all"):
                col = self._collection(col_name)
                if col is None:
                    continue
                col.upsert(ids=[doc_id], documents=[text], metadatas=[meta])
            return True
        except Exception:
            logger.debug(
                "Vector upsert failed for %s/%s", source, account_id, exc_info=True,
            )
            return False

    def search(
        self,
        query: str,
        source: Optional[str] = None,
        k: int = 20,
        where: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        """Semantic search; returns ranked accounts with score + excerpt."""
        if not self.available:
            return []
        col_name = self._collection_name(source) if source else "snapshots_all"
        col = self._collection(col_name)
        if col is None:
            return []
        try:
            res = col.query(
                query_texts=[query], n_results=max(1, min(k, 100)),
                where=where,
            )
        except Exception:
            logger.debug("Vector search failed", exc_info=True)
            return []

        out: list[dict[str, Any]] = []
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        for i, doc_id in enumerate(ids):
            meta = metas[i] if i < len(metas) else {}
            out.append({
                "id": doc_id,
                "source": (meta or {}).get("source", source or ""),
                "account_id": (meta or {}).get("account_id", ""),
                "account_name": (meta or {}).get("account_name", ""),
                "score": _distance_to_score(dists[i] if i < len(dists) else None),
                "excerpt": _truncate(docs[i] if i < len(docs) else "", 320),
                "metadata": meta or {},
            })
        return out

    def similar_to(
        self,
        source: str,
        account_id: str,
        k: int = 10,
    ) -> list[dict[str, Any]]:
        """Nearest-neighbour search starting from an existing document."""
        if not self.available:
            return []
        col = self._collection(self._collection_name(source))
        if col is None:
            return []
        doc_id = self._doc_id(source, account_id)
        try:
            anchor = col.get(ids=[doc_id], include=["documents"])
        except Exception:
            return []
        docs = anchor.get("documents") or []
        if not docs or not docs[0]:
            return []
        anchor_text = docs[0]
        # Pull k+1 because the anchor itself will appear in the result set.
        results = self.search(anchor_text, source=source, k=k + 1)
        return [r for r in results if r.get("account_id") != account_id][:k]

    # ── Bulk operations ─────────────────────────────────────────────

    def backfill_from_db(self, db: Any) -> dict[str, int]:
        """Reindex every row of ``source_snapshots`` from SQLite."""
        if not self.available:
            return {"available": 0}
        cursor = db._conn().execute(
            "SELECT account_id, source, payload_json, fetched_at FROM source_snapshots",
        )
        counts: dict[str, int] = {s: 0 for s in _SOURCES}
        for row in cursor:
            try:
                payload = json.loads(row["payload_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            payload["_fetched_at"] = row["fetched_at"]
            account = db.get_account(row["account_id"])
            account_name = (account or {}).get("name") or ""
            if self.upsert_snapshot(row["source"], row["account_id"], account_name, payload):
                counts[row["source"]] = counts.get(row["source"], 0) + 1
        return counts


def _distance_to_score(distance: Optional[float]) -> float:
    """Translate Chroma's L2 distance to a 0..1 similarity-style score."""
    if distance is None:
        return 0.0
    try:
        return round(max(0.0, 1.0 / (1.0 + float(distance))), 4)
    except (TypeError, ValueError):
        return 0.0


def _truncate(text: str, n: int) -> str:
    if not text:
        return ""
    text = text.strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


# ──────────────────────────────────────────────────────────────────────
# Module-level singleton
# ──────────────────────────────────────────────────────────────────────

_singleton: Optional[VectorStore] = None
_singleton_lock = threading.Lock()


def get_vector_store() -> VectorStore:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = VectorStore()
    return _singleton


# ──────────────────────────────────────────────────────────────────────
# CLI entry — used by ``make reindex``
# ──────────────────────────────────────────────────────────────────────

def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Vector store maintenance tools (ChromaDB)",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="Reindex every source_snapshots row from SQLite into ChromaDB.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    )

    store = get_vector_store()
    if not store.available:
        logger.error(
            "ChromaDB is not available. Install dependencies: "
            "pip install chromadb sentence-transformers",
        )
        raise SystemExit(2)

    if args.backfill:
        from app.store.db import get_db
        counts = store.backfill_from_db(get_db())
        total = sum(counts.values())
        logger.info("Backfill complete: %d documents (%s)", total, counts)
        return

    parser.print_help()


if __name__ == "__main__":
    _main()
