"""Transactional local vector index and authoritative revision/serving metadata.

SQLite is intentional: content, chunk vectors and the active pointer commit in
one transaction. Search only joins active, eligible versions. This provides a
reliable small-corpus baseline and does not claim distributed scalability.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from src.contracts import Document, Evidence
from src.ingestion.chunking import Chunk
from src.ingestion.embeddings import tokens


class PublicationConflict(RuntimeError):
    pass


def _numeric_revision(value: str) -> int | None:
    return int(value) if str(value).isdigit() else None


def validate_vectors(vectors: list[list[float]], expected: int, dimension: int | None = None) -> int:
    if len(vectors) != expected or not vectors:
        raise ValueError("Embedding count does not match chunk count")
    size = dimension or len(vectors[0])
    if not size or any(len(v) != size or any(not math.isfinite(n) for n in v) for v in vectors):
        raise ValueError("Embeddings must have one consistent dimension and finite values")
    return size


class SQLiteVectorStore:
    def __init__(self, path: str | Path = "data/index/documents.sqlite"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False, timeout=20)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS index_configuration(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS indexed_documents(
                doc_id TEXT PRIMARY KEY, source TEXT NOT NULL, revision TEXT NOT NULL,
                active_version TEXT, generation INTEGER NOT NULL DEFAULT 0,
                blocked INTEGER NOT NULL DEFAULT 0, digest TEXT, document_json TEXT,
                scope TEXT NOT NULL DEFAULT '', indexed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS indexed_chunks(
                chunk_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, version TEXT NOT NULL,
                source TEXT NOT NULL, content TEXT NOT NULL, vector TEXT NOT NULL,
                evidence_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS chunks_document ON indexed_chunks(doc_id, version);
            CREATE TABLE IF NOT EXISTS publication_events(
                event_id INTEGER PRIMARY KEY, doc_id TEXT NOT NULL,
                generation INTEGER NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL
            );
        """)
        self.connection.commit()

    def active_state(self, document_id: str) -> dict | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM indexed_documents WHERE doc_id=?", (document_id,)).fetchone()
            return dict(row) if row else None

    def assert_embedding_compatible(self, fingerprint: str, dimension: int | None = None):
        with self._lock:
            config = dict(self.connection.execute("SELECT key,value FROM index_configuration").fetchall())
        if config.get("embedding") not in (None, fingerprint):
            raise ValueError("Embedding configuration changed; build a new index for a coordinated cutover")
        if dimension and config.get("dimension") not in (None, str(dimension)):
            raise ValueError("Query/document embedding dimension differs from this index")

    def publish(self, document: Document, chunks: list[Chunk], vectors: list[list[float]], *,
                digest: str, expected_generation: int, embedding_id: str, scope: str = "") -> bool:
        size = validate_vectors(vectors, len(chunks))
        if not document.company_wide:
            raise ValueError("Restricted documents cannot be published")
        if not chunks or any(c.document_id != document.id or c.version != chunks[0].version for c in chunks):
            raise ValueError("Publication requires a complete single-document version")
        if len({c.id for c in chunks}) != len(chunks):
            raise ValueError("Chunk IDs must be unique")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self.connection:
            # BEGIN IMMEDIATE makes the generation check and write indivisible
            # even when separate worker/API connections use the same database.
            self.connection.execute("BEGIN IMMEDIATE")
            self.assert_embedding_compatible(embedding_id, size)
            current = self.active_state(document.id)
            if (current["generation"] if current else 0) != expected_generation:
                raise PublicationConflict("Source generation changed while preparing this version")
            if current:
                old, new = _numeric_revision(current["revision"]), _numeric_revision(document.revision)
                if old is not None and new is not None and new < old:
                    raise PublicationConflict("An older source revision cannot replace the current revision")
                if current["blocked"] and not (old is not None and new is not None and new > old):
                    raise PublicationConflict("A revoked revision requires verified newer source metadata")
            for key, value in (("embedding", embedding_id), ("dimension", str(size))):
                self.connection.execute("INSERT OR IGNORE INTO index_configuration VALUES (?,?)", (key, value))
            for chunk, vector in zip(chunks, vectors):
                evidence = Evidence(id=chunk.id, source=chunk.source, source_id=chunk.document_id,
                                    title=chunk.title, text=chunk.content, url=chunk.url,
                                    updated_at=chunk.updated_at, version=chunk.version,
                                    location=chunk.location, metadata=chunk.metadata)
                self.connection.execute("INSERT OR REPLACE INTO indexed_chunks VALUES (?,?,?,?,?,?,?)",
                                        (chunk.id, chunk.document_id, chunk.version, chunk.source, chunk.content,
                                         json.dumps(vector, allow_nan=False), evidence.model_dump_json()))
            self.connection.execute("""INSERT INTO indexed_documents VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(doc_id) DO UPDATE SET source=excluded.source, revision=excluded.revision,
                active_version=excluded.active_version, generation=excluded.generation, blocked=0,
                digest=excluded.digest, document_json=excluded.document_json, scope=excluded.scope,
                indexed_at=excluded.indexed_at""",
                (document.id, str(document.source), str(document.revision), chunks[0].version,
                 expected_generation + 1, 0, digest, document.model_dump_json(), scope, now))
            self.connection.execute("DELETE FROM indexed_chunks WHERE doc_id=? AND version!=?", (document.id, chunks[0].version))
            self.connection.execute("INSERT INTO publication_events(doc_id,generation,action,created_at) VALUES (?,?,?,?)",
                                    (document.id, expected_generation + 1, "publish", now))
        return True

    def revoke(self, source_id: str, revision: str | None = None, reason: str = "restricted") -> bool:
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            current = self.active_state(source_id)
            if current:
                old, new = _numeric_revision(current["revision"]), _numeric_revision(revision or current["revision"])
                if old is not None and new is not None and new < old:
                    return False
            rev = str(revision or (current["revision"] if current else "1"))
            generation = (current["generation"] if current else 0) + 1
            now = datetime.now(timezone.utc).isoformat()
            self.connection.execute("""INSERT INTO indexed_documents VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(doc_id) DO UPDATE SET revision=excluded.revision,
                generation=excluded.generation, blocked=1, active_version=NULL, digest=NULL,
                document_json=NULL, indexed_at=excluded.indexed_at""",
                (source_id, current["source"] if current else source_id.split(":")[0], rev, None,
                 generation, 1, None, None, current["scope"] if current else "", now))
            self.connection.execute("DELETE FROM indexed_chunks WHERE doc_id=?", (source_id,))
            action = "delete" if reason == "deleted" else "revoke"
            self.connection.execute("INSERT INTO publication_events(doc_id,generation,action,created_at) VALUES (?,?,?,?)",
                                    (source_id, generation, action, now))
        return True

    def is_current(self, evidence: Evidence) -> bool:
        state = self.active_state(evidence.source_id)
        return bool(state and not state["blocked"] and state["active_version"] == evidence.version)

    def get_document(self, source_id: str) -> Document | None:
        state = self.active_state(source_id)
        if not state or state["blocked"] or not state["document_json"]:
            return None
        return Document.model_validate_json(state["document_json"])

    def list_documents(self) -> list[dict]:
        with self._lock:
            rows = self.connection.execute("""SELECT d.*,COUNT(c.chunk_id) AS chunk_count
                FROM indexed_documents d LEFT JOIN indexed_chunks c
                ON d.doc_id=c.doc_id AND d.active_version=c.version
                GROUP BY d.doc_id ORDER BY d.source,d.doc_id""").fetchall()
        result = []
        for row in rows:
            data = dict(row)
            document = json.loads(data.pop("document_json") or "{}")
            result.append({**data, "id": data["doc_id"], "title": document.get("title", data["doc_id"]),
                           "url": document.get("url", ""), "updated_at": document.get("updated_at"),
                           "company_wide": not bool(data["blocked"])})
        return result

    def counts(self) -> dict:
        with self._lock:
            active = self.connection.execute("SELECT COUNT(*) FROM indexed_documents WHERE blocked=0 AND active_version IS NOT NULL").fetchone()[0]
            blocked = self.connection.execute("SELECT COUNT(*) FROM indexed_documents WHERE blocked=1").fetchone()[0]
            by_source = dict(self.connection.execute("SELECT source,COUNT(*) FROM indexed_documents WHERE blocked=0 AND active_version IS NOT NULL GROUP BY source").fetchall())
        return {"documents": active, "chunks": self.count(), "blocked": blocked, "by_source": by_source}

    def count(self) -> int:
        with self._lock:
            return self.connection.execute("""SELECT COUNT(*) FROM indexed_chunks c JOIN indexed_documents d
                ON c.doc_id=d.doc_id AND c.version=d.active_version WHERE d.blocked=0""").fetchone()[0]

    def _cosine_scores(self, vector: list[float], rows) -> list[float]:
        query_norm = math.sqrt(sum(v * v for v in vector)) or 1
        scores = []
        for row in rows:
            stored = json.loads(row["vector"])
            norm = math.sqrt(sum(v * v for v in stored)) or 1
            scores.append(sum(a * b for a, b in zip(vector, stored)) / (norm * query_norm))
        return scores

    def search(self, vector: list[float], k: int = 6, sources: Iterable[str] | None = None,
               query_text: str = "") -> list[Evidence]:
        if k < 1:
            return []
        size = validate_vectors([vector], 1)
        with self._lock:
            config = dict(self.connection.execute("SELECT key,value FROM index_configuration").fetchall())
            if config.get("dimension") not in (None, str(size)):
                raise ValueError("Embedding dimension mismatch")
            sql = """SELECT c.* FROM indexed_chunks c JOIN indexed_documents d
                ON c.doc_id=d.doc_id AND c.version=d.active_version WHERE d.blocked=0"""
            params = []
            if sources is not None:
                source_list = list(sources)
                if not source_list:
                    return []
                sql += " AND c.source IN (" + ",".join("?" for _ in source_list) + ")"
                params.extend(source_list)
            rows = self.connection.execute(sql, params).fetchall()
        if not rows:
            return []
        query_terms = set(tokens(query_text))
        term_counts = [Counter(tokens(row["content"])) for row in rows]
        lengths = [sum(count.values()) for count in term_counts]
        average = sum(lengths) / len(lengths) or 1
        frequency = {term: sum(term in count for count in term_counts) for term in query_terms}
        cosine_scores = self._cosine_scores(vector, rows)
        scored = []
        for index, (row, counts) in enumerate(zip(rows, term_counts)):
            cosine = cosine_scores[index]
            bm25 = 0.0
            for term in query_terms:
                count = counts.get(term, 0)
                if count:
                    idf = math.log(1 + (len(rows) - frequency[term] + 0.5) / (frequency[term] + 0.5))
                    bm25 += idf * count * 2.2 / (count + 1.2 * (0.25 + 0.75 * lengths[index] / average))
            scored.append((index, max(-1., min(1., cosine)), bm25))
        semantic = sorted(scored, key=lambda item: (-item[1], rows[item[0]]["chunk_id"]))
        lexical = sorted((item for item in scored if item[2] > 0), key=lambda item: (-item[2], rows[item[0]]["chunk_id"]))
        rank = {item[0]: 1 / (60 + position) for position, item in enumerate(semantic, 1)}
        for position, item in enumerate(lexical, 1):
            rank[item[0]] += 1 / (60 + position)
        results = []
        for index, cosine, bm25 in sorted(scored, key=lambda item: (-rank[item[0]], -item[1], rows[item[0]]["chunk_id"]))[:k]:
            evidence = Evidence.model_validate_json(rows[index]["evidence_json"])
            evidence = evidence.model_copy(update={"score": cosine, "metadata": {**evidence.metadata,
                "bm25_score": bm25, "rank_score": rank[index], "matched_terms": sorted(query_terms & term_counts[index].keys()),
                "query_terms": len(query_terms)}})
            # A revocation may commit after the candidate query; check again.
            if self.is_current(evidence):
                results.append(evidence)
        return results

    def delete_docs(self, document_ids: Iterable[str]) -> int:
        return sum(self.revoke(document_id, reason="deleted") for document_id in document_ids)

    def persist(self):
        with self._lock:
            self.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def close(self):
        with self._lock:
            self.connection.close()
