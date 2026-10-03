"""Optional OpenSearch hybrid reference adapter with local serving authority.

Remote hits supply candidate IDs only. Evidence and cosine scores are loaded
from current approved SQLite records, so stale or corrupted remote payloads
cannot authorize content. This is a single-writer reference integration, not a
distributed production metadata service; see docs/ingestion-guide.md.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

from src.contracts import Evidence
from src.ingestion.embeddings import tokens

from .sqlite import PublicationConflict, SQLiteVectorStore, validate_vectors


def _remote_id(chunk_id: str) -> str:
    return hashlib.sha256(chunk_id.encode()).hexdigest()


class OpenSearchVectorStore(SQLiteVectorStore):
    def __init__(self, path="data/runtime/documents.sqlite", *, endpoint: str = "",
                 index_name: str = "engineering-atlas-v1", username: str = "", password: str = "",
                 ca_certs: str | None = None, approved_remote_processing: bool = False, client=None):
        if not approved_remote_processing:
            raise ValueError("Remote indexing requires approved_remote_processing=True")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,127}", index_name):
            raise ValueError("Invalid OpenSearch index name")
        if client is None:
            parts = urlsplit(endpoint)
            if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.path not in ("", "/") or parts.query or parts.fragment:
                raise ValueError("Provide an HTTPS OpenSearch origin without embedded credentials")
            if not username or not password:
                raise ValueError("Explicit OpenSearch credentials are required")
            try:
                from opensearchpy import OpenSearch
            except ImportError as exc:
                raise RuntimeError("Install the optional opensearch-py package") from exc
            options = dict(hosts=[{"host": parts.hostname, "port": parts.port or 443}],
                http_auth=(username, password), use_ssl=True, verify_certs=True,
                ssl_assert_hostname=True, timeout=10, max_retries=2, retry_on_timeout=True)
            if ca_certs:
                options["ca_certs"] = ca_certs
            client = OpenSearch(**options)
        self.client, self.index_name = client, index_name
        self._remote_ready = None
        super().__init__(path)
        with self._lock, self.connection:
            self.connection.execute("""CREATE TABLE IF NOT EXISTS remote_cleanup(
                source_id TEXT NOT NULL, version TEXT NOT NULL, PRIMARY KEY(source_id,version))""")

    def _ensure_remote_ready(self, dimension: int, embedding_id: str):
        desired = (dimension, embedding_id)
        if self._remote_ready == desired:
            return
        if not self.client.indices.exists(index=self.index_name):
            self.client.indices.create(index=self.index_name, body={
                "settings": {"number_of_shards": 1, "number_of_replicas": 1},
                "mappings": {"dynamic": "strict", "_meta": {"embedding": embedding_id}, "properties": {
                    "chunk_id": {"type": "keyword"}, "source_id": {"type": "keyword"},
                    "version": {"type": "keyword"}, "source": {"type": "keyword"},
                    "content": {"type": "text"}, "vector": {"type": "knn_vector", "dimension": dimension},
                }},
            })
        mapping = self.client.indices.get_mapping(index=self.index_name)[self.index_name]["mappings"]
        if mapping.get("_meta", {}).get("embedding") != embedding_id or mapping.get("properties", {}).get("vector", {}).get("dimension") != dimension:
            raise ValueError("Remote index embedding configuration is incompatible; create a new index")
        self._remote_ready = desired

    def _queue_cleanup(self, source_id, version):
        if not version:
            return
        with self._lock, self.connection:
            self.connection.execute("INSERT OR IGNORE INTO remote_cleanup VALUES (?,?)", (source_id, version))

    def publish(self, document, chunks, vectors, *, digest, expected_generation, embedding_id, scope=""):
        from src.policy.safety import safe_url, sanitize_text
        if not document.company_wide or any(sanitize_text(value) != value for value in [document.title, document.body, *[chunk.content for chunk in chunks]]):
            raise ValueError("Only approved, screened content may enter the remote index")
        safe_url(document.url)
        dimension = validate_vectors(vectors, len(chunks))
        self.assert_embedding_compatible(embedding_id, dimension)
        current = self.active_state(document.id)
        if (current["generation"] if current else 0) != expected_generation:
            raise PublicationConflict("Source generation changed before remote staging")
        self._ensure_remote_ready(dimension, embedding_id)
        version = chunks[0].version
        try:
            # Bound each external write independently of source document size.
            for start in range(0, len(chunks), 64):
                operations = []
                for chunk, vector in zip(chunks[start:start + 64], vectors[start:start + 64]):
                    operations.append({"index": {"_index": self.index_name, "_id": _remote_id(chunk.id)}})
                    operations.append({"chunk_id": chunk.id, "source_id": chunk.document_id,
                        "version": chunk.version, "source": chunk.source, "content": chunk.content, "vector": vector})
                body = "\n".join(json.dumps(operation, allow_nan=False) for operation in operations) + "\n"
                response = self.client.bulk(body=body, refresh=True)
                if response.get("errors"):
                    raise RuntimeError("OpenSearch rejected part of the candidate batch")
            count = self.client.count(index=self.index_name, body={"query": {"bool": {"filter": [
                {"term": {"source_id": document.id}}, {"term": {"version": version}}]}}})["count"]
            if count != len(chunks):
                raise RuntimeError("Remote candidate is not completely visible")
            published = super().publish(document, chunks, vectors, digest=digest,
                expected_generation=expected_generation, embedding_id=embedding_id, scope=scope)
        except Exception:
            self._queue_cleanup(document.id, version)
            raise
        if current and current["active_version"] != version:
            self._queue_cleanup(document.id, current["active_version"])
        return published

    def revoke(self, source_id, revision=None, reason="restricted"):
        current = self.active_state(source_id)
        # Local serving blocks commit before an external cleanup can fail.
        result = super().revoke(source_id, revision=revision, reason=reason)
        if result and current:
            self._queue_cleanup(source_id, current["active_version"])
        return result

    def cleanup_retired(self, *, ingestion_paused: bool = False) -> int:
        """Retry exact-version physical deletions in a maintenance window.

        Pausing the single writer prevents a candidate retry racing its cleanup.
        Unremoved remote records remain unservable under SQLite authority.
        """
        if not ingestion_paused:
            raise ValueError("Pause ingestion before running remote cleanup")
        with self._lock:
            tasks = self.connection.execute("SELECT * FROM remote_cleanup").fetchall()
        removed = 0
        for task in tasks:
            state = self.active_state(task["source_id"])
            if state and not state["blocked"] and state["active_version"] == task["version"]:
                continue
            response = self.client.delete_by_query(index=self.index_name, refresh=True, body={"query": {"bool": {"filter": [
                {"term": {"source_id": task["source_id"]}}, {"term": {"version": task["version"]}}]}}})
            if response.get("failures") or response.get("timed_out"):
                continue
            with self._lock, self.connection:
                self.connection.execute("DELETE FROM remote_cleanup WHERE source_id=? AND version=?", (task["source_id"], task["version"]))
            removed += 1
        return removed

    def search(self, vector, k=6, sources=None, query_text=""):
        if k < 1 or not any(vector):
            return []
        validate_vectors([vector], 1)
        source_values = list(sources) if sources is not None else None
        if source_values == []:
            return []
        if not self.count():
            return []
        with self._lock:
            config = dict(self.connection.execute("SELECT key,value FROM index_configuration").fetchall())
        self._ensure_remote_ready(len(vector), config["embedding"])
        filters = [{"terms": {"source": source_values}}] if source_values is not None else []
        base_query = {"bool": {"filter": filters}} if filters else {"match_all": {}}
        limit = min(100, max(20, k * 4))
        semantic = self.client.search(index=self.index_name, body={"size": limit, "_source": ["chunk_id"], "query": {
            "script_score": {"query": base_query, "script": {"lang": "knn", "source": "knn_score",
                "params": {"field": "vector", "query_value": vector, "space_type": "cosinesimil"}}}}})
        rankings = [semantic.get("hits", {}).get("hits", [])]
        if query_text:
            lexical = self.client.search(index=self.index_name, body={"size": limit, "_source": ["chunk_id"],
                "query": {"bool": {"must": [{"match": {"content": query_text}}], "filter": filters}}})
            rankings.append(lexical.get("hits", {}).get("hits", []))
        ranks = {}
        for ranking in rankings:
            for position, hit in enumerate(ranking, 1):
                identifier = hit.get("_source", {}).get("chunk_id")
                if not isinstance(identifier, str) or hit.get("_id") != _remote_id(identifier):
                    continue
                ranks[identifier] = ranks.get(identifier, 0.) + 1 / (60 + position)
        approved = []
        query_terms = set(tokens(query_text))
        for identifier, rank in sorted(ranks.items(), key=lambda value: (-value[1], value[0])):
            # Never trust the remote title, text, URL, version or score. Join the
            # authoritative active pointer and reconstruct from approved content.
            with self._lock:
                row = self.connection.execute("""SELECT c.* FROM indexed_chunks c JOIN indexed_documents d
                    ON c.doc_id=d.doc_id AND c.version=d.active_version
                    WHERE c.chunk_id=? AND d.blocked=0""", (identifier,)).fetchone()
            if row is None or (source_values is not None and row["source"] not in source_values):
                continue
            evidence = Evidence.model_validate_json(row["evidence_json"])
            score = self._cosine_scores(vector, [row])[0]
            matched = sorted(query_terms & set(tokens(evidence.text)))
            evidence = evidence.model_copy(update={"score": max(-1., min(1., score)), "metadata": {
                **evidence.metadata, "rank_score": rank, "matched_terms": matched, "query_terms": len(query_terms)}})
            if self.is_current(evidence):
                approved.append(evidence)
            if len(approved) >= k:
                break
        return approved

    def close(self):
        try:
            if hasattr(self.client, "close"):
                self.client.close()
        finally:
            super().close()
