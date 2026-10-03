"""Durable revision jobs, cursor transactions and fenced expiring leases."""
from __future__ import annotations

import hashlib
import time
import uuid
from typing import Callable

from src.contracts import Document
from src.vectorstore.sqlite import SQLiteVectorStore

from .pipeline import IngestionPipeline


class IngestionJobQueue:
    def __init__(self, store: SQLiteVectorStore):
        self.store = store
        with store._lock, store.connection:
            store.connection.executescript("""
                CREATE TABLE IF NOT EXISTS ingestion_jobs(
                    job_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, revision TEXT NOT NULL,
                    action TEXT NOT NULL, scope TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL DEFAULT 0, failure_code TEXT, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_checkpoints(scope TEXT PRIMARY KEY, cursor TEXT NOT NULL);
            """)

    def enqueue(self, source_id: str, revision: str, *, action: str = "update", scope: str = "",
                cursor: str | None = None) -> str:
        if action not in {"update", "delete", "restrict"}:
            raise ValueError("Unknown source event action")
        identity = hashlib.sha256(f"{scope}\0{source_id}\0{revision}\0{action}".encode()).hexdigest()
        with self.store._lock, self.store.connection:
            self.store.connection.execute("BEGIN IMMEDIATE")
            self.store.connection.execute("""INSERT OR IGNORE INTO ingestion_jobs
                (job_id,source_id,revision,action,scope,created_at) VALUES (?,?,?,?,?,?)""",
                (identity, source_id, str(revision), action, scope, time.time()))
            if cursor is not None:
                # Advancing a source cursor cannot get ahead of durable work.
                self.store.connection.execute("INSERT OR REPLACE INTO source_checkpoints VALUES (?,?)", (scope, cursor))
        return identity

    def checkpoint(self, scope: str) -> str | None:
        with self.store._lock:
            row = self.store.connection.execute("SELECT cursor FROM source_checkpoints WHERE scope=?", (scope,)).fetchone()
        return row[0] if row else None

    def claim(self, lease_seconds: float = 30) -> dict | None:
        now, token = time.time(), uuid.uuid4().hex
        with self.store._lock, self.store.connection:
            self.store.connection.execute("BEGIN IMMEDIATE")
            row = self.store.connection.execute("""SELECT * FROM ingestion_jobs
                WHERE (state='pending' OR (state='leased' AND lease_until<?)) AND available_at<=?
                ORDER BY created_at,job_id LIMIT 1""", (now, now)).fetchone()
            if not row:
                return None
            self.store.connection.execute("""UPDATE ingestion_jobs SET state='leased',lease_token=?,
                lease_until=?,attempts=attempts+1 WHERE job_id=?""", (token, now + lease_seconds, row["job_id"]))
            return {**dict(row), "lease_token": token, "attempts": row["attempts"] + 1}

    def finish(self, job: dict, failure_code: str | None = None) -> bool:
        now = time.time()
        state = "done" if failure_code is None else "failed" if job["attempts"] >= 4 else "pending"
        with self.store._lock, self.store.connection:
            cursor = self.store.connection.execute("""UPDATE ingestion_jobs SET state=?,failure_code=?,
                available_at=?,lease_until=0,lease_token=NULL WHERE job_id=? AND lease_token=? AND state='leased'""",
                (state, failure_code, now + (2 ** job["attempts"] if failure_code else 0), job["job_id"], job["lease_token"]))
            return cursor.rowcount == 1

    def counts(self) -> dict[str, int]:
        with self.store._lock:
            return dict(self.store.connection.execute("SELECT state,COUNT(*) FROM ingestion_jobs GROUP BY state").fetchall())

    def work_once(self, fetch_current: Callable[[str], Document | None], pipeline: IngestionPipeline) -> bool:
        """Always fetch trusted current metadata; an event alone is not authority."""
        job = self.claim()
        if job is None:
            return False
        try:
            document = fetch_current(job["source_id"])
            if document is None:
                if job["action"] != "delete":
                    raise ValueError("Source unavailable without a verified delete event")
                self.store.revoke(job["source_id"], revision=job["revision"], reason="deleted")
            else:
                if document.id != job["source_id"]:
                    raise ValueError("Current source identity does not match this job")
                stats = pipeline.ingest([document], scope=job["scope"])
                if stats.failures:
                    raise ValueError("Source was not published")
            self.finish(job)
        except Exception as exc:
            self.finish(job, type(exc).__name__)
        return True
