"""Drive adapter with an injected, authenticated transport and trusted ACLs.

The transport must enumerate a configured scope and return current API metadata,
including company_wide. This flag must never come from document content. Native
Google documents should be exported as text/plain by read_bytes().
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Protocol

from src.contracts import Document
from src.ingestion.extractors import extract_bytes
from src.policy.safety import safe_url

from .base import BaseConnector


class DriveTransport(Protocol):
    def list_items(self, folder_id: str) -> Iterable[dict]: ...
    def read_bytes(self, item: dict) -> bytes: ...


class GoogleDriveConnector(BaseConnector):
    source = "drive"

    def __init__(self, transport: DriveTransport, folder_id: str):
        super().__init__()
        self.transport, self.folder_id = transport, folder_id

    @property
    def scope(self):
        return f"drive:folder:{self.folder_id}"

    def iter_documents(self):
        self.complete = False
        self.failures.clear()
        try:
            for item in self.transport.list_items(self.folder_id):
                identifier = str(item.get("id", ""))
                try:
                    if not identifier or not item.get("version"):
                        raise ValueError("Drive item lacks stable identity or revision")
                    eligible = item.get("company_wide") is True and not item.get("trashed", False)
                    # Restricted records are yielded without fetching raw content,
                    # allowing the pipeline to block a previously public revision.
                    body, blocks = "", []
                    if eligible:
                        suffix = ".txt" if item.get("mimeType") == "application/vnd.google-apps.document" else Path(item["name"]).suffix
                        result = extract_bytes(self.transport.read_bytes(item), suffix)
                        body, blocks = result.text, result.blocks
                    yield Document(id=f"drive:{identifier}", source="drive", title=item.get("name", identifier),
                        body=body, url=safe_url(item.get("webViewLink", "")), updated_at=item.get("modifiedTime"),
                        revision=str(item["version"]), company_wide=eligible,
                        metadata={"drive_id": identifier, "blocks": blocks, "format": item.get("mimeType", ""),
                                  "eligibility_version": str(item.get("permission_version", item["version"]))})
                except Exception as exc:
                    self.failures.append((identifier, type(exc).__name__))
        except Exception as exc:
            self.failures.append((self.folder_id, type(exc).__name__))
        self.complete = not self.failures


DriveConnector = GoogleDriveConnector
