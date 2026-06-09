"""
JSON-file-backed store for CXM/TAM custom risk factors.

Each risk factor is a dict with a UUID, linked to an account_id, carrying
a category from the predefined catalog (or freeform 'other'), severity,
title, notes, status, and audit timestamps.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STORE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "custom_risk_factors.json"

RISK_CATEGORIES: list[dict[str, str]] = [
    {"id": "champion_loss", "group": "Relationship", "label": "Key champion/sponsor departing"},
    {"id": "executive_change", "group": "Relationship", "label": "CIO/CTO/IT Director change"},
    {"id": "stakeholder_dissatisfaction", "group": "Relationship", "label": "Stakeholder dissatisfaction"},
    {"id": "no_executive_engagement", "group": "Relationship", "label": "No executive engagement / QBRs"},
    {"id": "single_thread", "group": "Relationship", "label": "Single-threaded relationship"},
    {"id": "upgrade_blocked", "group": "Technical", "label": "Critical upgrade blocked or refused"},
    {"id": "capacity_concern", "group": "Technical", "label": "Cluster capacity approaching limits"},
    {"id": "performance_issues", "group": "Technical", "label": "Recurring performance complaints"},
    {"id": "dr_gap", "group": "Technical", "label": "No DR / business-continuity plan"},
    {"id": "migration_risk", "group": "Technical", "label": "Migration away from Nutanix planned"},
    {"id": "integration_issues", "group": "Technical", "label": "Ecosystem integration issues"},
    {"id": "shadow_it", "group": "Technical", "label": "Shadow IT / competing infra procurement"},
    {"id": "budget_pressure", "group": "Commercial", "label": "Budget cuts or spending freeze"},
    {"id": "competitive_threat", "group": "Commercial", "label": "Active competitor POC / evaluation"},
    {"id": "pricing_concern", "group": "Commercial", "label": "Pricing or value objections raised"},
    {"id": "procurement_delay", "group": "Commercial", "label": "Expansion / renewal stuck in procurement"},
    {"id": "partial_churn", "group": "Commercial", "label": "Planned footprint reduction"},
    {"id": "low_feature_adoption", "group": "Engagement", "label": "Licensed features not being used"},
    {"id": "training_gap", "group": "Engagement", "label": "Customer team lacks training"},
    {"id": "no_qbr_cadence", "group": "Engagement", "label": "No regular QBR cadence established"},
    {"id": "poor_nps_feedback", "group": "Engagement", "label": "Poor NPS / CSAT survey feedback"},
    {"id": "support_friction", "group": "Engagement", "label": "Support experience frustration"},
    {"id": "ma_activity", "group": "External", "label": "M&A / restructuring in progress"},
    {"id": "regulatory_change", "group": "External", "label": "Regulatory change affecting infra"},
    {"id": "other", "group": "Other", "label": "Other (freeform)"},
]

_CATEGORY_MAP = {c["id"]: c for c in RISK_CATEGORIES}


class CustomRiskStore:
    """Thread-safe JSON-file store for custom risk factors."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _STORE_PATH
        self._lock = threading.Lock()
        self._data: dict[str, list[dict]] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path, "r") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not load custom risks: %s", exc)
                self._data = {}
        else:
            self._data = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self._data, f, indent=2)
        tmp.replace(self._path)

    def list_for_account(self, account_id: str) -> list[dict]:
        with self._lock:
            return list(self._data.get(account_id, []))

    def add(
        self,
        account_id: str,
        category: str,
        severity: str,
        title: str,
        notes: str = "",
        created_by: str = "",
    ) -> dict:
        if category not in _CATEGORY_MAP and category != "other":
            category = "other"
        if severity not in ("critical", "high", "medium", "low"):
            severity = "medium"
        if not title and category in _CATEGORY_MAP:
            title = _CATEGORY_MAP[category]["label"]

        entry: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "account_id": account_id,
            "category": category,
            "severity": severity,
            "title": title,
            "notes": notes,
            "created_by": created_by,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "active",
            "resolved_at": None,
        }
        with self._lock:
            self._data.setdefault(account_id, []).append(entry)
            self._save()
        return entry

    def update(self, account_id: str, risk_id: str, updates: dict) -> dict | None:
        allowed = {"severity", "title", "notes", "status"}
        with self._lock:
            items = self._data.get(account_id, [])
            for item in items:
                if item["id"] == risk_id:
                    for k, v in updates.items():
                        if k in allowed:
                            item[k] = v
                    if updates.get("status") in ("resolved", "mitigated"):
                        item["resolved_at"] = datetime.now(timezone.utc).isoformat()
                    self._save()
                    return dict(item)
        return None

    def delete(self, account_id: str, risk_id: str) -> bool:
        with self._lock:
            items = self._data.get(account_id, [])
            before = len(items)
            self._data[account_id] = [i for i in items if i["id"] != risk_id]
            if len(self._data[account_id]) < before:
                self._save()
                return True
        return False

    @staticmethod
    def get_categories() -> list[dict[str, str]]:
        return list(RISK_CATEGORIES)
