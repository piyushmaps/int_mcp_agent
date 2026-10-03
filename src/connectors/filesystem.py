from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from src.contracts import Document
from src.ingestion.extractors import SUPPORTED_SUFFIXES, extract_file

from .base import BaseConnector


class LocalDirectoryConnector(BaseConnector):
    """Read an explicitly approved local root without following paths outside it.

    Eligibility is trusted connector configuration, never front matter supplied
    inside a document. A failed read makes enumeration incomplete for pruning.
    """
    def __init__(self, root: str | Path, source: str = "devportal", company_wide: bool = True):
        super().__init__()
        self.root = Path(root).resolve()
        self.source = source
        self.company_wide = company_wide

    @property
    def scope(self) -> str:
        return f"local:{self.source}:{hashlib.sha256(str(self.root).encode()).hexdigest()[:16]}"

    def iter_documents(self) -> Iterator[Document]:
        self.complete = False
        self.failures.clear()
        if not self.root.is_dir():
            self.failures.append((self.root.name, "Source directory does not exist"))
            return
        try:
            files = sorted(self.root.rglob("*"))
            for path in files:
                if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue
                relative = path.relative_to(self.root).as_posix()
                try:
                    resolved = path.resolve(strict=True)
                    if not resolved.is_relative_to(self.root):
                        raise ValueError("Source path resolves outside its approved root")
                    extracted = extract_file(resolved)
                    stat = resolved.stat()
                    document_id = f"{self.source}:{relative}"
                    yield Document(id=document_id, source=self.source,
                        title=extracted.title or path.stem.replace("-", " ").replace("_", " ").title(),
                        body=extracted.text,
                        url=f"/api/documents/{quote(document_id, safe='')}/content",
                        updated_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
                        revision=str(stat.st_mtime_ns), company_wide=self.company_wide,
                        metadata={"path": relative, "format": path.suffix.lower(), "blocks": extracted.blocks,
                                  "eligibility_version": "connector-config-v1"})
                except Exception as exc:
                    # Never log raw parser errors or source text: both can include
                    # sensitive material. The class is sufficient for diagnostics.
                    self.failures.append((relative, type(exc).__name__))
        except OSError as exc:
            self.failures.append((self.root.name, type(exc).__name__))
        self.complete = not self.failures


LocalConnector = LocalDirectoryConnector
