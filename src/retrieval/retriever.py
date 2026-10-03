"""Bounded evidence retrieval with relevance and active-version validation."""
from __future__ import annotations

import asyncio

from src.contracts import Evidence
from src.ingestion.embeddings import HashEmbeddings
from src.policy.safety import UnsafeContentError, safe_url, sanitize_text
from src.vectorstore import SQLiteVectorStore

_UNSET = object()


class SourceRetriever:
    def __init__(self, store=None, embeddings=None, top_k: int = 6, min_similarity: float = 0.28):
        self.store = store if store is not None else SQLiteVectorStore("data/runtime/documents.sqlite")
        self.embeddings = embeddings if embeddings is not None else HashEmbeddings()
        self.top_k, self.min_similarity = top_k, min_similarity

    def retrieve(self, query: str, sources=None, k=None, min_similarity=_UNSET) -> list[Evidence]:
        query = sanitize_text(query)
        if not query:
            return []
        self.store.assert_embedding_compatible(self.embeddings.fingerprint)
        limit = min(20, max(1, k if k is not None else self.top_k))
        threshold = self.min_similarity if min_similarity is _UNSET else min_similarity
        source_values = [getattr(source, "value", str(source)) for source in sources] if sources is not None else None
        vector = self.embeddings.embed_query(query)
        candidates = self.store.search(vector, k=min(80, limit * 4), sources=source_values, query_text=query)
        results, seen = [], set()
        for hit in candidates:
            if not self.store.is_current(hit):
                continue
            terms = hit.metadata.get("matched_terms", [])
            query_count = hit.metadata.get("query_terms", 0)
            # The offline model has no semantic understanding. Exact matching
            # terms rescue identifiers whose cosine is diluted by a long chunk;
            # accidental hashing collisions cannot rescue unrelated results.
            lexical_match = bool(terms) and (len(terms) >= 2 or query_count == 1) and len(terms) / max(1, query_count) >= .34
            if threshold is not None:
                if isinstance(self.embeddings, HashEmbeddings) and not terms:
                    continue
                if hit.score < threshold and not lexical_match:
                    continue
            try:
                text, title, location = sanitize_text(hit.text), sanitize_text(hit.title), sanitize_text(hit.location)
                url = safe_url(hit.url)
            except UnsafeContentError:
                continue
            fingerprint = (hit.source_id, text)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            results.append(hit.model_copy(update={"text": text, "title": title, "location": location, "url": url}))
            if len(results) >= limit:
                break
        return results

    async def aretrieve(self, query, sources=None, k=None, min_similarity=_UNSET):
        return await asyncio.to_thread(self.retrieve, query, sources, k, min_similarity)
