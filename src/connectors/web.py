"""Allowlisted HTTPS fetches with DNS pinning, bounded bodies and redirects."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import socket
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from src.contracts import Document
from src.ingestion.extractors import MAX_FILE_BYTES, extract_html
from src.policy.safety import safe_url

from .base import BaseConnector


def fetch_approved_url(url: str, allowed_hosts: set[str], timeout: float = 10,
                       max_bytes: int = MAX_FILE_BYTES) -> tuple[bytes, str]:
    allowed_hosts = {host.lower().rstrip(".") for host in allowed_hosts}
    deadline = time.monotonic() + timeout
    for _ in range(4):
        url = safe_url(url)
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme != "https" or host not in allowed_hosts or parts.port not in (None, 443) or parts.username or parts.password:
            raise ValueError("URL is outside the approved HTTPS host scope")
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
            raise ValueError("URL resolves to a non-public network address")
        # Use the validated address for the actual connection. A second DNS lookup
        # would create a rebinding gap. TLS still verifies the original hostname.
        address = addresses[0][4]
        raw_socket = socket.socket(addresses[0][0], socket.SOCK_STREAM)
        raw_socket.settimeout(timeout)
        connection = http.client.HTTPSConnection(host, timeout=timeout, context=ssl.create_default_context())
        try:
            raw_socket.connect(address)
            tls_socket = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=host)
            connection.sock = tls_socket
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            connection.request("GET", path, headers={"User-Agent": "KnowledgeDesk/1.0", "Accept": "text/html,text/plain", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Redirect has no destination")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError("Source returned an unsuccessful HTTP status")
            content_type = response.getheader("Content-Type", "").split(";")[0].strip().lower()
            if content_type not in {"text/html", "text/plain", "application/xhtml+xml"}:
                raise ValueError("Web connector only accepts text or HTML")
            if response.getheader("Content-Encoding", "identity").lower() not in {"identity", ""}:
                raise ValueError("Compressed web responses are not accepted")
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Web source exceeded its deadline")
                tls_socket.settimeout(min(timeout, remaining))
                chunk = response.read1(min(64 * 1024, max_bytes + 1 - size))
                if not chunk:
                    return b"".join(chunks), url
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("Web response exceeds the byte limit")
                chunks.append(chunk)
        finally:
            connection.close()
            raw_socket.close()
    raise ValueError("Web source exceeds the redirect limit")


class WebConnector(BaseConnector):
    source = "web"

    def __init__(self, urls: list[str], allowed_hosts: set[str], company_wide: bool = False):
        super().__init__()
        self.urls, self.allowed_hosts, self.company_wide = urls, allowed_hosts, company_wide

    @property
    def scope(self):
        return "web:" + hashlib.sha256("\n".join(sorted(self.urls)).encode()).hexdigest()[:16]

    def iter_documents(self):
        self.failures.clear()
        self.complete = False
        for url in self.urls:
            try:
                content, canonical = fetch_approved_url(url, self.allowed_hosts)
                result = extract_html(content.decode("utf-8"))
                identity = hashlib.sha256(url.encode()).hexdigest()[:20]
                yield Document(id=f"web:{identity}", source=self.source, title=result.title or urlsplit(canonical).hostname,
                    body=result.text, url=canonical, updated_at=None,
                    revision=hashlib.sha256(content).hexdigest(), company_wide=self.company_wide,
                    metadata={"format": "html", "fetched_at": datetime.now(timezone.utc).isoformat()})
            except Exception as exc:
                self.failures.append((hashlib.sha256(url.encode()).hexdigest()[:12], type(exc).__name__))
        self.complete = not self.failures
