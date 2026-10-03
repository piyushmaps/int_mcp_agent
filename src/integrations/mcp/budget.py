"""A request-scoped ledger shared by all source branches and refinements."""

from __future__ import annotations

import asyncio
import time


class BudgetExhausted(RuntimeError):
    pass


class CallBudget:
    def __init__(self, max_calls: int = 8, timeout_seconds: float = 25.0, concurrency: int = 2):
        if max_calls < 0 or timeout_seconds <= 0 or concurrency < 1:
            raise ValueError("Invalid request budget")
        self.max_calls = max_calls
        self.used = 0
        self.deadline = time.monotonic() + timeout_seconds
        self._lock = asyncio.Lock()
        self.semaphore = asyncio.Semaphore(concurrency)

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    async def reserve(self) -> None:
        """Charge every attempt, including retries and failed/uncertain calls."""
        async with self._lock:
            if self.remaining_seconds() <= 0:
                raise TimeoutError("Request deadline expired")
            if self.used >= self.max_calls:
                raise BudgetExhausted("Request tool budget exhausted")
            self.used += 1
