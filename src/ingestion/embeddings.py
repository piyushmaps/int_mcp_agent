"""Offline embeddings for a reproducible demo; explicit OpenAI opt-in."""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Protocol

STOP_WORDS = frozenset("a an and are as at be by can do does for from how i in is it me of on or that the their this to was what when where which who with you your".split())


def tokens(text: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", text.lower())
            if token not in STOP_WORDS and len(token) > 1]


class Embeddings(Protocol):
    fingerprint: str
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class HashEmbeddings:
    """Signed feature hashing, not a learned semantic model or an API call.

    Its score is cosine similarity; keyword/BM25 ranking complements it. Python's
    randomized hash() is deliberately avoided so vectors survive restarts.
    """
    def __init__(self, dimension: int = 384):
        if dimension < 32:
            raise ValueError("Embedding dimension must be at least 32")
        self.dimension = dimension
        self.fingerprint = f"signed-token-hash-v1:{dimension}"

    def embed_query(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token, count in Counter(tokens(text)).items():
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            vector[index] += (1.0 if digest[4] & 1 else -1.0) * (1 + math.log(count))
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(text) for text in texts]


class OpenAIEmbeddingAdapter:
    def __init__(self, api_key: str, model: str = "text-embedding-3-small", dimensions: int = 1536,
                 approved_processing: bool = False):
        if not approved_processing:
            raise ValueError("Remote embeddings require approved_processing=True")
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key, timeout=20, max_retries=2)
        self.model, self.dimension = model, dimensions
        self.fingerprint = f"openai:{model}:{dimensions}"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        from src.policy.safety import sanitize_text
        if not texts or len(texts) > 128 or sum(len(text.encode("utf-8")) for text in texts) > 100_000:
            raise ValueError("Embedding request exceeds the bounded batch limits")
        if any(len(text.encode("utf-8")) > 8_000 for text in texts):
            raise ValueError("Embedding input exceeds the conservative token-safe byte limit")
        if any(sanitize_text(text) != text for text in texts):
            raise ValueError("Only already-screened text can enter the embedding provider")
        response = self.client.embeddings.create(model=self.model, dimensions=self.dimension, input=texts)
        return [item.embedding for item in sorted(response.data, key=lambda item: item.index)]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]
