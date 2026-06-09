"""
Optional event bus between connectors and downstream stores.

By default the analyzer fans snapshots out *directly* into SQLite (via
the worker's ``upsert_source_snapshot``) and ChromaDB (via
``Scheduler._publish_snapshot``). This module adds an optional pub/sub
layer on top, gated by ``EVENT_BUS_ENABLED`` so production stays
zero-config.

Why this exists
---------------
The user asked us to gauge Memphis.dev for the workflow. Honest take:
Memphis was rebranded to Superstream and the Python SDK (``memphis-py``)
is frozen at v1.3.1 (Jan 2024). For a single-tenant analyzer running on
a 24h cadence the streaming benefit is mostly architectural: replay,
decoupling, multi-consumer fan-out (alerting, Slack, webhook). To avoid
locking onto an unmaintained SDK we expose three interchangeable
backends behind an abstract ``EventBus`` interface:

* ``NullBus``    — no-op; the default and what runs in CI.
* ``MemphisBus`` — uses ``memphis-py`` if installed and ``memphis``
                   is selected.
* ``NatsBus``    — uses ``nats-py`` (Memphis runs on NATS JetStream
                   under the hood; this is the maintained path).

Subjects/stations are ``{prefix}.{source}`` (e.g. ``snapshots.planhat``).

The bus is best-effort — every publish call is wrapped in a logger so a
broker outage never wedges the scheduler.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Interface
# ──────────────────────────────────────────────────────────────────────

class EventBus:
    """Abstract pub/sub surface used by the scheduler."""

    enabled: bool = False
    backend: str = "null"

    async def publish(self, source: str, message: dict[str, Any]) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        return None


# ──────────────────────────────────────────────────────────────────────
# Backends
# ──────────────────────────────────────────────────────────────────────

class NullBus(EventBus):
    enabled = False
    backend = "null"

    async def publish(self, source: str, message: dict[str, Any]) -> None:
        return None


class NatsBus(EventBus):
    """NATS JetStream backend — the maintained Memphis substrate."""

    backend = "nats"

    def __init__(self, url: str, subject_prefix: str) -> None:
        self.url = url
        self.subject_prefix = subject_prefix
        self.enabled = True
        self._nc: Any = None

    async def _ensure_connected(self) -> Any:
        if self._nc is not None and not self._nc.is_closed:
            return self._nc
        try:
            import nats  # type: ignore
        except Exception as exc:
            logger.warning("nats-py not available: %s", exc)
            self.enabled = False
            return None
        try:
            self._nc = await nats.connect(self.url)
            logger.info("Connected to NATS at %s", self.url)
        except Exception as exc:
            logger.warning("NATS connect to %s failed: %s", self.url, exc)
            self.enabled = False
            self._nc = None
        return self._nc

    async def publish(self, source: str, message: dict[str, Any]) -> None:
        nc = await self._ensure_connected()
        if nc is None:
            return
        subject = f"{self.subject_prefix}.{source}"
        try:
            await nc.publish(subject, json.dumps(message, default=str).encode())
        except Exception as exc:
            logger.warning("NATS publish %s failed: %s", subject, exc)

    async def close(self) -> None:
        if self._nc is None:
            return
        try:
            await self._nc.drain()
        except Exception:
            pass
        self._nc = None


class MemphisBus(EventBus):
    """
    Memphis.dev backend (via ``memphis-py``).

    Note: memphis-py is frozen at v1.3.1 (Jan 2024) and is not officially
    maintained anymore — this backend is provided for users who already
    operate a Memphis cluster. New deployments should prefer NatsBus.
    """

    backend = "memphis"

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        account_id: int,
        subject_prefix: str,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.account_id = account_id
        self.subject_prefix = subject_prefix
        self.enabled = True
        self._client: Any = None
        self._producers: dict[str, Any] = {}

    async def _ensure_connected(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from memphis import Memphis  # type: ignore
        except Exception as exc:
            logger.warning(
                "memphis-py not installed; install with `pip install memphis-py` "
                "or switch EVENT_BUS_BACKEND to 'nats' (%s)", exc,
            )
            self.enabled = False
            return None
        try:
            client = Memphis()
            await client.connect(
                host=self.host,
                username=self.username,
                password=self.password,
                account_id=self.account_id,
            )
            self._client = client
            logger.info("Connected to Memphis at %s", self.host)
        except Exception as exc:
            logger.warning("Memphis connect to %s failed: %s", self.host, exc)
            self.enabled = False
            self._client = None
        return self._client

    async def _producer(self, station: str) -> Any:
        client = await self._ensure_connected()
        if client is None:
            return None
        existing = self._producers.get(station)
        if existing is not None:
            return existing
        try:
            producer = await client.producer(
                station_name=station, producer_name="risk-analyzer-scheduler",
            )
            self._producers[station] = producer
            return producer
        except Exception as exc:
            logger.warning("Memphis producer for %s failed: %s", station, exc)
            return None

    async def publish(self, source: str, message: dict[str, Any]) -> None:
        station = f"{self.subject_prefix}.{source}"
        producer = await self._producer(station)
        if producer is None:
            return
        try:
            await producer.produce(
                message=json.dumps(message, default=str).encode(),
            )
        except Exception as exc:
            logger.warning("Memphis publish %s failed: %s", station, exc)

    async def close(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            await client.close()
        except Exception:
            pass
        self._client = None
        self._producers.clear()


# ──────────────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────────────

def build_event_bus() -> Optional[EventBus]:
    """Construct the bus backend selected by env, or a NullBus."""
    if not settings.event_bus_enabled:
        return NullBus()

    backend = (settings.event_bus_backend or "null").lower()
    prefix = settings.event_bus_subject_prefix or "snapshots"

    if backend == "nats":
        return NatsBus(url=settings.nats_url, subject_prefix=prefix)
    if backend == "memphis":
        return MemphisBus(
            host=settings.memphis_host,
            port=settings.memphis_port,
            username=settings.memphis_username,
            password=settings.memphis_password,
            account_id=settings.memphis_account_id,
            subject_prefix=prefix,
        )
    if backend == "null":
        return NullBus()
    logger.warning(
        "Unknown EVENT_BUS_BACKEND=%r; defaulting to NullBus", backend,
    )
    return NullBus()
