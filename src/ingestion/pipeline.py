"""Incremental ingestion that screens before embedding and publishes atomically."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from src.contracts import Document
from src.policy.safety import UnsafeContentError, safe_url, sanitize_text
from src.vectorstore.sqlite import PublicationConflict, SQLiteVectorStore, validate_vectors

from .chunking import CHUNKER_VERSION, chunk_document
from .embeddings import Embeddings, HashEmbeddings

PARSER_VERSION = "located-extractors-v1"
REDACTION_VERSION = "screening-v1"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class IngestionStats:
    documents_seen: int = 0
    documents_skipped: int = 0
    documents_indexed: int = 0
    documents_deleted: int = 0
    documents_quarantined: int = 0
    chunks_written: int = 0
    batches: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    elapsed_seconds: float = 0
    complete: bool = True

    def model_dump(self):
        return asdict(self)

    def render(self):
        return json.dumps(self.model_dump(), indent=2)


def normalize_document(document: Document) -> Document:
    if not document.id.strip() or not document.source.strip() or len(document.id) > 1024:
        raise UnsafeContentError("invalid_source_identity")
    if sanitize_text(document.id) != document.id or sanitize_text(document.source) != document.source:
        raise UnsafeContentError("unsafe_source_identity")
    title, body = sanitize_text(document.title), sanitize_text(document.body)
    if not body:
        raise ValueError("Document has no readable text")
    metadata = {}
    # Keep only located extraction blocks and simple metadata. Screening is not
    # optional for headings or metadata: they can otherwise bypass the body gate.
    for key, value in document.metadata.items():
        key = sanitize_text(str(key))
        if key == "blocks" and isinstance(value, list):
            metadata[key] = [{"text": sanitize_text(str(block.get("text", ""))),
                              "location": sanitize_text(str(block.get("location", "")))}
                             for block in value if isinstance(block, dict)]
        elif isinstance(value, str):
            metadata[key] = sanitize_text(value)
        elif isinstance(value, (int, float, bool, type(None))):
            metadata[key] = value
    return document.model_copy(update={"title": title, "body": body, "url": safe_url(document.url), "metadata": metadata})


class IngestionPipeline:
    def __init__(self, store: SQLiteVectorStore, embeddings: Embeddings | None = None,
                 chunk_size: int = 800, overlap: int = 120, batch_size: int = 64,
                 max_batch_chars: int = 100_000):
        self.store = store
        self.embeddings = embeddings if embeddings is not None else HashEmbeddings()
        self.chunk_size, self.overlap = chunk_size, overlap
        if batch_size < 1 or max_batch_chars < chunk_size:
            raise ValueError("Embedding batch bounds must fit at least one chunk")
        self.batch_size, self.max_batch_chars = batch_size, max_batch_chars

    def _digest(self, document):
        # Permissions, revision, locations and processing choices all participate.
        payload = {"document": document.model_dump(mode="json"), "parser": PARSER_VERSION,
                   "chunker": CHUNKER_VERSION, "size": self.chunk_size, "overlap": self.overlap,
                   "redaction": REDACTION_VERSION, "embedding": self.embeddings.fingerprint}
        # Connector observation time changes without changing source content.
        payload["document"]["metadata"].pop("fetched_at", None)
        return content_hash(json.dumps(payload, sort_keys=True, ensure_ascii=False))

    def _embed(self, chunks, stats):
        result = []
        batch, characters = [], 0
        for chunk in chunks:
            if batch and (len(batch) >= self.batch_size or characters + len(chunk.content) > self.max_batch_chars):
                vectors = self.embeddings.embed_documents(batch)
                validate_vectors(vectors, len(batch))
                result.extend(vectors)
                stats.batches += 1
                batch, characters = [], 0
            batch.append(chunk.content)
            characters += len(chunk.content)
        if batch:
            vectors = self.embeddings.embed_documents(batch)
            validate_vectors(vectors, len(batch))
            result.extend(vectors)
            stats.batches += 1
        validate_vectors(result, len(chunks))
        return result

    def ingest(self, documents: Iterable[Document], *, full_refresh: bool = False,
               prune: bool = False, scope: str | None = None, complete: bool = True) -> IngestionStats:
        if prune and not scope:
            raise ValueError("Pruning requires an exact, explicitly enumerated connector scope")
        start = time.monotonic()
        stats = IngestionStats(complete=complete)
        seen = set()
        self.store.assert_embedding_compatible(self.embeddings.fingerprint)
        try:
            for document in documents:
                stats.documents_seen += 1
                seen.add(document.id)
                state = self.store.active_state(document.id)
                # A verified restriction wins even if text or a revision hash did
                # not change. Restricted raw content never reaches the sanitizer,
                # embedding client, logs or the persisted content store.
                if not document.company_wide:
                    self.store.revoke(document.id, revision=document.revision)
                    stats.documents_quarantined += 1
                    continue
                try:
                    approved = normalize_document(document)
                except UnsafeContentError:
                    self.store.revoke(document.id, revision=document.revision, reason="quarantined")
                    stats.documents_quarantined += 1
                    stats.failures.append((document.id, "content_screening_failed"))
                    stats.complete = False
                    continue
                except (ValueError, TypeError):
                    stats.failures.append((document.id, "normalization_failed"))
                    stats.complete = False
                    continue
                digest = self._digest(approved)
                if not full_refresh and state and not state["blocked"] and state["digest"] == digest:
                    stats.documents_skipped += 1
                    continue
                version = f"{document.revision}-{digest[:16]}"
                chunks = chunk_document(approved, self.chunk_size, self.overlap, version)
                if not chunks:
                    stats.failures.append((document.id, "empty_extraction"))
                    stats.complete = False
                    continue
                # Service failures abort this run; previous active versions stay
                # available and a later retry skips already committed documents.
                vectors = self._embed(chunks, stats)
                try:
                    self.store.publish(approved, chunks, vectors, digest=digest,
                        expected_generation=state["generation"] if state else 0,
                        embedding_id=self.embeddings.fingerprint, scope=scope or "")
                except PublicationConflict:
                    stats.failures.append((document.id, "source_changed_or_revoked"))
                    stats.complete = False
                    continue
                stats.documents_indexed += 1
                stats.chunks_written += len(chunks)
        except Exception:
            # No pruning on interrupted enumeration or transient provider failure.
            stats.complete = False
            raise
        if prune and stats.complete:
            for known in self.store.list_documents():
                if known["scope"] == scope and not known["blocked"] and known["doc_id"] not in seen:
                    stats.documents_deleted += self.store.revoke(known["doc_id"], reason="deleted")
        stats.elapsed_seconds = round(time.monotonic() - start, 4)
        self.store.persist()
        return stats

    def ingest_connectors(self, connectors, *, full_refresh=False, prune=True):
        aggregate = IngestionStats()
        started = time.monotonic()
        for connector in connectors:
            seen = set()
            def records():
                for document in connector.iter_documents():
                    seen.add(document.id)
                    yield document
            stats = self.ingest(records(), full_refresh=full_refresh, scope=connector.scope)
            stats.failures.extend(connector.failures)
            stats.complete = stats.complete and connector.complete
            if prune and stats.complete:
                for known in self.store.list_documents():
                    if known["scope"] == connector.scope and not known["blocked"] and known["doc_id"] not in seen:
                        stats.documents_deleted += self.store.revoke(known["doc_id"], reason="deleted")
            for name in ("documents_seen", "documents_skipped", "documents_indexed", "documents_deleted",
                         "documents_quarantined", "chunks_written", "batches"):
                setattr(aggregate, name, getattr(aggregate, name) + getattr(stats, name))
            aggregate.failures.extend(stats.failures)
            aggregate.complete = aggregate.complete and stats.complete
        aggregate.elapsed_seconds = round(time.monotonic() - started, 4)
        self.store.persist()
        return aggregate


def build_connectors(root: str | Path = "data/documents"):
    from src.connectors import LocalDirectoryConnector
    root = Path(root)
    subdirectories = sorted(path for path in root.iterdir() if path.is_dir()) if root.is_dir() else []
    if subdirectories:
        return [LocalDirectoryConnector(path, source=path.name) for path in subdirectories]
    return [LocalDirectoryConnector(root)]


def iter_all_documents(connectors):
    for connector in connectors:
        yield from connector.iter_documents()


def run_ingestion(connectors=None, store=None, embeddings=None, *, full_refresh=False, prune=True,
                  root="data/documents", index_path="data/runtime/documents.sqlite"):
    owned = store is None
    store = store if store is not None else SQLiteVectorStore(index_path)
    try:
        return IngestionPipeline(store, embeddings).ingest_connectors(
            connectors if connectors is not None else build_connectors(root), full_refresh=full_refresh, prune=prune)
    finally:
        if owned:
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="data/documents")
    parser.add_argument("--index", default="data/runtime/documents.sqlite")
    parser.add_argument("--full-refresh", action="store_true")
    parser.add_argument("--no-prune", action="store_true")
    parser.add_argument("--demo", action="store_true", help="Ingest the bundled synthetic demo records")
    arguments = parser.parse_args()
    if arguments.demo:
        from src.bootstrap import demo_documents
        with_store = SQLiteVectorStore(arguments.index)
        try:
            candidates = [doc for doc in demo_documents()
                          if not (with_store.active_state(doc.id) or {}).get("blocked")]
            stats = IngestionPipeline(with_store).ingest(candidates, full_refresh=arguments.full_refresh, scope="demo")
        finally:
            with_store.close()
        print(stats.render())
        raise SystemExit(0 if stats.complete else 1)
    if arguments.root == "data/documents" and not Path(arguments.root).exists():
        parser.error("No data/documents directory exists. Use --demo for synthetic fixtures or --root for an approved folder.")
    stats = run_ingestion(root=arguments.root, index_path=arguments.index,
                          full_refresh=arguments.full_refresh, prune=not arguments.no_prune)
    print(stats.render())
    raise SystemExit(0 if stats.complete else 1)


if __name__ == "__main__":
    main()
