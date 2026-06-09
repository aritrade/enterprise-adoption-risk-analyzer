"""
AI self-heal layer for ingestion workers — failure-only, local-Ollama-backed.

Workers in ``app/sync/`` call :func:`diagnose_failure` after they catch an
exception or detect malformed/empty output. The function asks a local
LLM running under Ollama (default ``gemma4:latest``) for a constrained
JSON verdict that classifies the failure and proposes a recovery action.
The worker then chooses whether to retry, escalate, or fall back to its
last-known-good snapshot.

Design constraints:

* No LLM call on the happy path — never invoked when a worker succeeds.
* Strict JSON output: Ollama's ``format`` parameter is set to a JSON
  schema mirroring :data:`VERDICT_SCHEMA`, which constrains decoding so
  the model cannot emit prose, fences, or off-schema fields.
* Cost-bounded: response inputs are truncated to a fixed byte budget so
  a single failure can't blow through model context.
* Fully optional: if the Ollama daemon is unreachable or the configured
  model tag is missing, :func:`diagnose_failure` returns a deterministic
  :data:`DEFAULT_VERDICT` so workers still operate. The liveness check
  is cached for 60 s and shared with the ``/api/status`` endpoint.

Every invocation — including the ones that no-op due to a missing
daemon — is logged into the ``sync_runs.ai_actions_json`` audit column
by the calling worker.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


_RESPONSE_BYTE_BUDGET = 1500
_PROMPT = """\
You are the diagnostic agent for a single Python ingestion worker that
just failed to fetch data from {source}. Choose ONE verdict that best
fits the failure, and emit a strict JSON object — no prose, no markdown
fences.

Allowed verdicts:
- transient_retry: timeout, 5xx, network blip; retry with backoff.
- auth_expired:    401/403, redirect to login, missing cookie/token.
- schema_drift:    response shape changed, fields missing/renamed.
- rate_limited:    429 or explicit throttle.
- upstream_down:   the source is genuinely unavailable (long outage).
- abort:           anything else not safe to auto-retry.

Output schema (return EXACTLY this JSON, with these keys):
{{
  "verdict": "<one of the verdicts above>",
  "confidence": <float 0-1>,
  "fix_proposal": {{}},          // optional; only for schema_drift, may name selectors/fields
  "human_summary": "<one sentence describing what likely happened>"
}}

Worker context:
  source:           {source}
  worker_kind:      {kind}
  account_id:       {account_id}
  expected_schema:  {schema}
  exception_class:  {exc_class}
  exception_msg:    {exc_msg}
  recent_failures:  {recent_failures}  (count of failures in last 24h)
  response_excerpt: {response_excerpt}
"""


VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "confidence", "human_summary"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": [
                "transient_retry",
                "auth_expired",
                "schema_drift",
                "rate_limited",
                "upstream_down",
                "abort",
            ],
        },
        "confidence":    {"type": "number", "minimum": 0, "maximum": 1},
        "fix_proposal":  {"type": "object"},
        "human_summary": {"type": "string"},
    },
}


DEFAULT_VERDICT: dict[str, Any] = {
    "verdict": "transient_retry",
    "confidence": 0.0,
    "fix_proposal": {},
    "human_summary": "AI heal disabled or unavailable; defaulting to a single retry then fall back to last-known-good snapshot.",
    "ai_disabled": True,
}


@dataclass
class FailureContext:
    source: str
    kind: str = "account"             # 'index' | 'account' | 'portfolio'
    account_id: str = ""
    expected_schema: str = ""         # short human description of expected shape
    exception: Optional[BaseException] = None
    response_excerpt: str = ""
    recent_failures: int = 0
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealAction:
    """Audit record for what the heal layer decided to do."""
    source: str
    account_id: str
    verdict: str
    confidence: float
    fix_proposal: dict[str, Any]
    human_summary: str
    invoked_at: str
    duration_ms: int
    ai_disabled: bool = False
    raw_response: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "account_id": self.account_id,
            "verdict": self.verdict,
            "confidence": round(self.confidence, 3),
            "fix_proposal": self.fix_proposal,
            "human_summary": self.human_summary,
            "invoked_at": self.invoked_at,
            "duration_ms": self.duration_ms,
            "ai_disabled": self.ai_disabled,
        }


def _truncate(text: str, limit: int = _RESPONSE_BYTE_BUDGET) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit // 2] + " ...[truncated]... " + text[-limit // 2 :]


# ── Ollama liveness probe ────────────────────────────────────────────

_OLLAMA_PROBE: dict[str, Any] = {"ready": False, "checked_at": 0.0}
_OLLAMA_PROBE_TTL = 60.0  # seconds


def is_ollama_ready(force: bool = False) -> bool:
    """
    Cheap readiness check shared by :func:`diagnose_failure` and
    ``/api/status``.

    Returns ``True`` only when (a) the Ollama daemon answers on
    ``OLLAMA_BASE_URL`` and (b) the configured ``OLLAMA_MODEL`` tag is
    present in the daemon's model catalogue. Result is cached for
    :data:`_OLLAMA_PROBE_TTL` seconds so the dashboard's once-a-minute
    status poll never costs more than one HTTP round-trip.
    """
    now = time.time()
    if not force and now - _OLLAMA_PROBE["checked_at"] < _OLLAMA_PROBE_TTL:
        return bool(_OLLAMA_PROBE["ready"])
    ready = False
    try:
        with httpx.Client(timeout=1.5) as client:
            resp = client.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags")
            resp.raise_for_status()
            tags = {(m.get("name") or "") for m in resp.json().get("models", [])}
        ready = settings.ollama_model in tags
        if not ready:
            logger.debug(
                "Ollama reachable but model %s not in catalogue (%d tags)",
                settings.ollama_model, len(tags),
            )
    except Exception as exc:
        logger.debug("Ollama readiness probe failed: %s", exc)
        ready = False
    _OLLAMA_PROBE.update({"ready": ready, "checked_at": now})
    return ready


# ── LLM call ─────────────────────────────────────────────────────────

def _call_ollama(prompt: str) -> str:
    """
    Send the diagnostic prompt to Ollama with constrained JSON decoding.

    Returns the raw assistant-message string. Raises on transport or
    HTTP errors so the caller can record a deterministic default
    verdict in ``HealAction``.

    ``think: false`` disables the model's reasoning trace — Gemma 4 and
    other reasoning models otherwise split output between ``thinking``
    and ``content``, and a long enough prompt can exhaust the prediction
    budget on the reasoning half and leave ``content`` empty.
    """
    payload = {
        "model":  settings.ollama_model,
        "stream": False,
        "think":  False,
        "format": VERDICT_SCHEMA,
        "options": {"temperature": 0, "num_predict": 800},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a careful diagnostic agent. Output strict "
                    "JSON only — no prose, no markdown fences."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
    body = resp.json()
    return (body.get("message") or {}).get("content", "") or ""


def _parse_verdict(raw: str) -> dict[str, Any]:
    """
    Parse the model response into a verdict dict.

    With Ollama's ``format=<schema>`` the response should already be
    valid JSON, but we keep the regex fallback as belt-and-suspenders
    so a misbehaving build never crashes the worker.
    """
    raw = (raw or "").strip()
    if not raw:
        return dict(DEFAULT_VERDICT)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", raw)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return {**DEFAULT_VERDICT, "human_summary": "AI returned unparseable response — defaulting to retry."}


# ── Public entry point ──────────────────────────────────────────────

def diagnose_failure(ctx: FailureContext) -> HealAction:
    """
    Ask the AI heal layer to diagnose ``ctx``. Always returns a HealAction.

    On any error (network, parse, missing daemon) the function logs and
    returns a HealAction with ``verdict='transient_retry'`` so the
    worker has a deterministic path forward.
    """
    started = time.time()
    invoked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))

    if not settings.ai_heal_enabled:
        return HealAction(
            source=ctx.source,
            account_id=ctx.account_id,
            verdict=DEFAULT_VERDICT["verdict"],
            confidence=0.0,
            fix_proposal={},
            human_summary="AI heal disabled via config (ai_heal_enabled=false).",
            invoked_at=invoked_at,
            duration_ms=0,
            ai_disabled=True,
        )

    if not is_ollama_ready():
        return HealAction(
            source=ctx.source,
            account_id=ctx.account_id,
            verdict=DEFAULT_VERDICT["verdict"],
            confidence=0.0,
            fix_proposal={},
            human_summary=(
                f"Ollama daemon or model '{settings.ollama_model}' not "
                f"available at {settings.ollama_base_url} — heal layer no-op."
            ),
            invoked_at=invoked_at,
            duration_ms=0,
            ai_disabled=True,
        )

    exc_class = ctx.exception.__class__.__name__ if ctx.exception else "None"
    exc_msg = str(ctx.exception) if ctx.exception else ""
    prompt = _PROMPT.format(
        source=ctx.source,
        kind=ctx.kind,
        account_id=ctx.account_id or "(none)",
        schema=ctx.expected_schema or "(unspecified)",
        exc_class=exc_class,
        exc_msg=_truncate(exc_msg, 400),
        recent_failures=ctx.recent_failures,
        response_excerpt=_truncate(ctx.response_excerpt),
    )

    try:
        raw = _call_ollama(prompt)
    except Exception as exc:
        logger.warning(
            "AI heal call failed for %s/%s: %s — defaulting to transient_retry",
            ctx.source, ctx.account_id, exc,
        )
        return HealAction(
            source=ctx.source,
            account_id=ctx.account_id,
            verdict=DEFAULT_VERDICT["verdict"],
            confidence=0.0,
            fix_proposal={},
            human_summary=f"Ollama call failed: {type(exc).__name__}",
            invoked_at=invoked_at,
            duration_ms=int((time.time() - started) * 1000),
            ai_disabled=False,
        )

    parsed = _parse_verdict(raw)
    duration_ms = int((time.time() - started) * 1000)
    return HealAction(
        source=ctx.source,
        account_id=ctx.account_id,
        verdict=str(parsed.get("verdict") or DEFAULT_VERDICT["verdict"]),
        confidence=float(parsed.get("confidence") or 0.0),
        fix_proposal=parsed.get("fix_proposal") or {},
        human_summary=str(parsed.get("human_summary") or ""),
        invoked_at=invoked_at,
        duration_ms=duration_ms,
        ai_disabled=False,
        raw_response=_truncate(raw, 800),
    )
