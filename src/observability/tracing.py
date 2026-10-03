import threading
from collections import deque
from datetime import datetime, timezone


class Telemetry:
    """Only allowlisted metadata. No questions, source text, prompts, credentials or exceptions."""
    def __init__(self):
        self.events = deque(maxlen=500)
        self._lock = threading.Lock()

    def record(self, *, actor: str, request_id: str, status: str, duration_ms: int, sources: list[str]):
        with self._lock:
            self.events.appendleft({"id": request_id, "actor": actor, "status": status,
                                   "duration_ms": duration_ms, "sources": sources,
                                   "created_at": datetime.now(timezone.utc).isoformat(),
                                   "question": "Engineering question"})

    def list(self, actor: str):
        with self._lock:
            return [{k: v for k, v in e.items() if k != "actor"} for e in self.events if e["actor"] == actor]

