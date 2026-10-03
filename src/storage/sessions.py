"""Bounded temporary conversation context; never authoritative evidence or an answer cache."""
import threading
import time
from uuid import uuid4

from src.contracts import AnswerResult


class SessionDenied(LookupError):
    pass

class SessionStore:
    def __init__(self, ttl: int = 3600, max_turns: int = 6, max_sessions: int = 1000):
        self.ttl, self.max_turns, self.max_sessions = ttl, max_turns, max_sessions
        self._sessions: dict[str, dict] = {}
        self._lock = threading.RLock()

    def _purge(self):
        now = time.time()
        for sid in list(self._sessions):
            if self._sessions[sid]["expires_at"] <= now:
                del self._sessions[sid]

    def authorize(self, actor: str, session_id: str | None) -> str:
        with self._lock:
            self._purge()
            if session_id:
                row = self._sessions.get(session_id)
                if row is None or row["owner"] != actor:
                    raise SessionDenied("session_unavailable")
                return session_id
            if len(self._sessions) >= self.max_sessions:
                raise SessionDenied("session_capacity")
            sid = uuid4().hex
            self._sessions[sid] = {"id": sid, "owner": actor, "title": "New conversation", "messages": [],
                                   "updated_at": time.time(), "expires_at": time.time() + self.ttl,
                                   "dependencies": set()}
            return sid

    def load(self, actor: str, sid: str) -> dict:
        with self._lock:
            self.authorize(actor, sid)
            row = self._sessions[sid]
            return {"id": sid, "title": row["title"], "messages": list(row["messages"]),
                    "updated_at": row["updated_at"]}

    def save(self, actor: str, sid: str, question: str, result: AnswerResult):
        if result.status not in {"answered", "partial", "insufficient_evidence"}:
            return
        with self._lock:
            self.authorize(actor, sid)
            row = self._sessions[sid]
            if not row["messages"]:
                row["title"] = question[:80]
            row["messages"].extend([
                {"role": "user", "content": question},
                {"role": "assistant", "content": result.answer,
                 "citations": [c.model_dump(mode="json") for c in result.citations],
                 "status": result.status, "limitations": result.limitations},
            ])
            row["messages"] = row["messages"][-self.max_turns * 2:]
            row["dependencies"].update(c.source_id for c in result.citations)
            row["updated_at"] = time.time()
            row["expires_at"] = time.time() + self.ttl

    def list(self, actor: str) -> list[dict]:
        with self._lock:
            self._purge()
            return sorted([{"id": r["id"], "title": r["title"], "updated_at": r["updated_at"]}
                           for r in self._sessions.values() if r["owner"] == actor and r["messages"]],
                          key=lambda r: r["updated_at"], reverse=True)

    def delete(self, actor: str, sid: str):
        with self._lock:
            self.authorize(actor, sid)
            del self._sessions[sid]

    def invalidate(self, source_id: str):
        with self._lock:
            for sid in list(self._sessions):
                if source_id in self._sessions[sid]["dependencies"]:
                    del self._sessions[sid]

