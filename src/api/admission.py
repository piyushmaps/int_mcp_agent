import asyncio
from collections import Counter

from fastapi import HTTPException


class Admission:
    """Process-local limits for the single-replica reference deployment."""
    def __init__(self, global_limit, user_limit):
        self.global_limit, self.user_limit = global_limit, user_limit
        self.active = Counter()
        self.lock = asyncio.Lock()

    async def acquire(self, actor):
        async with self.lock:
            if sum(self.active.values()) >= self.global_limit or self.active[actor] >= self.user_limit:
                raise HTTPException(429, "request_capacity_reached", headers={"Retry-After": "2"})
            self.active[actor] += 1

    async def release(self, actor):
        async with self.lock:
            self.active[actor] -= 1
            if self.active[actor] <= 0:
                del self.active[actor]

