"""Deterministic bounded chunks retaining source and section locations."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.contracts import Document

CHUNKER_VERSION = "located-characters-v1"


@dataclass(frozen=True)
class Chunk:
    id: str
    document_id: str
    version: str
    source: str
    title: str
    content: str
    url: str
    updated_at: Any = None
    location: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def _pieces(text: str, limit: int, overlap: int):
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            # Prefer a complete paragraph, sentence or word, but enforce a hard
            # bound even for a single giant token or malformed source line.
            floor = start + limit // 2
            for separator in ("\n\n", ". ", "\n", " "):
                boundary = text.rfind(separator, floor, end)
                if boundary >= floor:
                    end = boundary + len(separator)
                    break
        piece = text[start:end].strip()
        if piece:
            yield piece
        if end == len(text):
            return
        start = max(start + 1, end - overlap)


def chunk_document(document: Document, chunk_size: int = 800,
                   overlap: int = 120, version: str | None = None) -> list[Chunk]:
    if chunk_size < 128 or overlap < 0 or overlap >= chunk_size // 2:
        raise ValueError("Use chunk_size >= 128 and overlap < half the chunk size")
    version = version or str(document.revision)
    raw_blocks = document.metadata.get("blocks")
    if isinstance(raw_blocks, list) and raw_blocks:
        blocks = [(str(b.get("text", "")), str(b.get("location", "")))
                  for b in raw_blocks if isinstance(b, dict)]
    else:
        blocks = []
        heading = ""
        for section in re.split(r"(?=^#{1,6}\s)", document.body, flags=re.MULTILINE):
            match = re.match(r"^(#{1,6})\s+(.+)", section)
            if match:
                heading = match.group(2).strip()
            blocks.append((section, heading))
    chunks = []
    # Reserve bounded title context for each independently retrieved chunk.
    prefix = f"[{str(document.source).upper()}] {document.title[:100]}\n\n"
    limit = chunk_size - len(prefix)
    if limit < 32:
        prefix = prefix[: min(64, chunk_size // 3)] + "\n"
        limit = chunk_size - len(prefix)
    overlap = min(overlap, max(0, limit // 3))
    metadata = {key: value for key, value in document.metadata.items()
                if key != "blocks" and isinstance(value, (str, int, float, bool, type(None)))}
    for text, location in blocks:
        for piece in _pieces(text, limit, overlap):
            index = len(chunks)
            chunks.append(Chunk(
                id=f"{document.id}:{version}#{index}", document_id=document.id,
                version=version, source=str(document.source), title=document.title,
                content=prefix + piece, url=document.url, updated_at=document.updated_at,
                location=location, metadata={**metadata, "chunk_index": index},
            ))
    return chunks
