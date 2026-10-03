"""Bounded, current dependency probes with a short per-process cache.

Discovery verifies authentication, protocol negotiation and approved catalogs.
It does not query source records or charge an employee's content-read budget.
"""

from __future__ import annotations

import asyncio
import time


class Readiness:
    def __init__(self, engine, store, cache_seconds: float = 5.0, timeout_seconds: float = 3.0):
        self.engine = engine
        self.store = store
        self.cache_seconds = cache_seconds
        self.timeout_seconds = timeout_seconds
        self._cached: dict | None = None
        self._cached_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._cached = None
        self._cached_at = 0.0

    async def _source(self, source: str) -> str:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with self.engine.client(source) as client:
                    if not client.tools:
                        return "unavailable"
            return "ready"
        except Exception:
            return "unavailable"

    async def check(self) -> dict:
        async with self._lock:
            if self._cached is not None and time.monotonic() - self._cached_at < self.cache_seconds:
                return {"status": self._cached["status"], "services": dict(self._cached["services"])}
            jira, slack = await asyncio.gather(self._source("jira"), self._source("slack"))
            try:
                documents = "ready" if self.store.counts().get("chunks", 0) > 0 else "empty"
            except Exception:
                documents = "unavailable"
            services = {"jira": jira, "slack": slack, "documents": documents}
            result = {"status": "ok" if all(value == "ready" for value in services.values()) else "degraded",
                      "services": services}
            self._cached = result
            self._cached_at = time.monotonic()
            return {"status": result["status"], "services": dict(services)}
