"""Read-only Google Drive v3 transport with explicit scopes and current domain ACL checks."""
import hashlib
import json
import re

import httpx

from src.ingestion.extractors import MAX_FILE_BYTES


class GoogleDriveHTTPTransport:
    """Supply a short-lived approved OAuth token; this adapter never writes Drive data.

    The organization must approve both the folder and each item. Current API
    permissions must additionally include company-domain access. An allowlist
    alone never turns a private Drive file into company-wide content.
    """
    origin = "https://www.googleapis.com/drive/v3"

    def __init__(self, access_token: str, *, company_domain: str, approved_folders: set[str],
                 approved_items: set[str], client=None, max_pages: int = 20):
        if not access_token or not company_domain or not approved_folders or not 1 <= max_pages <= 100:
            raise ValueError("A token, company domain, approved folder scope and bounded pagination are required")
        self.company_domain = company_domain.casefold()
        self.folders, self.items = set(approved_folders), set(approved_items)
        self.max_pages = max_pages
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self.client = client or httpx.Client(timeout=15, follow_redirects=False, trust_env=False)
        self._owns_client = client is None
        self._listed = set()

    def _json(self, path, params):
        with self.client.stream("GET", self.origin + path, params=params, headers=self._headers) as response:
            response.raise_for_status()
            data = self._read(response, 2_000_000)
        return json.loads(data)

    @staticmethod
    def _read(response, limit):
        parts, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > limit:
                raise ValueError("Drive response exceeds byte limit")
            parts.append(chunk)
        return b"".join(parts)

    @staticmethod
    def _identifier(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("Invalid Drive source identity")
        return value

    def _permissions(self, identifier):
        permissions, cursor = [], None
        for _ in range(self.max_pages):
            params = {"fields": "nextPageToken,permissions(type,domain,role,deleted)", "supportsAllDrives": "true", "pageSize": 100}
            if cursor:
                params["pageToken"] = cursor
            data = self._json(f"/files/{self._identifier(identifier)}/permissions", params)
            permissions.extend(data.get("permissions", []))
            cursor = data.get("nextPageToken")
            if not cursor:
                eligible = any(p.get("type") == "domain" and p.get("domain", "").casefold() == self.company_domain
                               and p.get("role") in {"reader", "commenter", "writer", "fileOrganizer", "organizer", "owner"}
                               and not p.get("deleted", False) for p in permissions)
                version = hashlib.sha256(json.dumps(permissions, sort_keys=True).encode()).hexdigest()
                return eligible, version
        raise ValueError("Drive permission enumeration is incomplete")

    def list_items(self, folder_id):
        if folder_id not in self.folders:
            raise ValueError("Drive folder is outside the approved scope")
        self._identifier(folder_id)
        cursor = None
        for _ in range(self.max_pages):
            params = {"q": f"'{folder_id}' in parents and trashed = false", "pageSize": 100,
                      "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
                      "fields": "nextPageToken,incompleteSearch,files(id,name,mimeType,modifiedTime,version,webViewLink,trashed)"}
            if cursor:
                params["pageToken"] = cursor
            page = self._json("/files", params)
            if page.get("incompleteSearch"):
                raise ValueError("Drive folder enumeration is incomplete")
            for item in page.get("files", []):
                identifier = self._identifier(item["id"])
                allowed, version = self._permissions(identifier) if identifier in self.items else (False, "unapproved")
                self._listed.add(identifier)
                yield {**item, "company_wide": allowed, "permission_version": version}
            cursor = page.get("nextPageToken")
            if not cursor:
                return
        raise ValueError("Drive folder exceeds page limit")

    def read_bytes(self, item):
        identifier = self._identifier(item["id"])
        if identifier not in self.items or identifier not in self._listed or not self._permissions(identifier)[0]:
            raise ValueError("Drive content is not currently eligible")
        native = item.get("mimeType") == "application/vnd.google-apps.document"
        path = f"/files/{identifier}/export" if native else f"/files/{identifier}"
        params = {"mimeType": "text/plain"} if native else {"alt": "media", "supportsAllDrives": "true"}
        with self.client.stream("GET", self.origin + path, params=params, headers=self._headers) as response:
            response.raise_for_status()
            return self._read(response, MAX_FILE_BYTES)

    def close(self):
        if self._owns_client:
            self.client.close()

