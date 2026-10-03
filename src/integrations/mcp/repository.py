"""Deterministic local fixture repository with server-side record authorization.

This is synthetic data, not a provider or OAuth implementation. Eligibility is
owned by the fixture store, never by text or caller-supplied scope assertions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .catalog import IssueArguments, SearchArguments, ThreadArguments

FIXTURE_DIRECTORY = Path(__file__).resolve().parents[3] / "data" / "fixtures"
ALLOWED_PROJECTS = frozenset({"PAY", "OPS"})
ALLOWED_CHANNELS = frozenset({"C_PAYMENTS", "C_GENERAL"})
MAX_RESPONSE_BYTES = 256_000
MAX_RECORD_BYTES = 16_000
_CURSOR_KEY = b"local-synthetic-fixture-cursor-v1"
_STOPWORDS = frozenset("the a an is are was why what when where how which and or of to for in on its it with this status guide please tell me about delayed delay".split())


class FixtureRepository:
    def __init__(self, source: str, fixture_directory: Path | None = None):
        if source not in {"jira", "slack"}:
            raise ValueError("Unsupported fixture source")
        self.source = source
        self.path = (fixture_directory or FIXTURE_DIRECTORY) / f"{source}.json"

    def records(self) -> list[dict]:
        records = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(records, list) or len(records) > 10_000:
            raise ValueError("Invalid fixture store")
        return records

    def eligible(self, record: dict) -> bool:
        if record.get("eligibility", {}).get("audience") != "company_wide":
            return False
        if record.get("deleted", False):
            return False
        if self.source == "jira":
            return record.get("issue_key", "").split("-")[0] in ALLOWED_PROJECTS
        return record.get("channel_id") in ALLOWED_CHANNELS

    def identity(self, record: dict) -> str:
        return record["issue_key" if self.source == "jira" else "message_id"]

    def current(self, source_id: str) -> dict | None:
        return next((r for r in self.records() if self.identity(r) == source_id and self.eligible(r)), None)

    def _cursor(self, binding: str, offset: int) -> str:
        payload = json.dumps([self.source, binding, "company_wide", offset], separators=(",", ":")).encode()
        signature = hmac.new(_CURSOR_KEY, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(signature + payload).decode()

    def _offset(self, binding: str, cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            decoded = base64.b64decode(cursor, altchars=b"-_", validate=True)
            signature, payload = decoded[:32], decoded[32:]
            if not hmac.compare_digest(signature, hmac.new(_CURSOR_KEY, payload, hashlib.sha256).digest()):
                raise ValueError()
            source, original, scope, offset = json.loads(payload)
            if source != self.source or original != binding or scope != "company_wide" or not isinstance(offset, int) or offset < 0:
                raise ValueError()
            return offset
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError("Invalid pagination cursor") from exc

    def _page(self, records: list[dict], binding: str, limit: int, cursor: str | None, field: str = "items") -> dict:
        offset = self._offset(binding, cursor)
        page = records[offset:offset + limit]
        for record in page:
            if len(json.dumps(record).encode()) > MAX_RECORD_BYTES:
                raise ValueError("Source record exceeds the permitted size")
        next_offset = offset + len(page)
        truncated = next_offset < len(records)
        payload = {field: page, "next_cursor": self._cursor(binding, next_offset) if truncated else None,
                   "truncated": truncated, "observed_at": datetime.now(timezone.utc).isoformat()}
        if len(json.dumps(payload).encode()) > MAX_RESPONSE_BYTES:
            raise ValueError("Source response exceeds the permitted size")
        return payload

    def search(self, query: str, limit: int = 10, cursor: str | None = None) -> dict:
        args = SearchArguments(query=query, limit=limit, cursor=cursor)
        keys = re.findall(r"\b[A-Z][A-Z0-9]{1,15}-[1-9][0-9]*\b", args.query.upper())
        terms = [w for w in re.findall(r"[a-z0-9]+", args.query.lower()) if w not in _STOPWORDS]
        ranked = []
        for record in self.records():
            if not self.eligible(record):
                continue
            haystack = " ".join(str(record.get(key, "")) for key in ("issue_key", "title", "description", "text", "team", "status")).lower()
            if keys and not any(key.lower() in haystack for key in keys):
                continue
            score = sum(term in haystack for term in terms)
            if score or keys:
                ranked.append((score, self.identity(record), record))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return self._page([r[2] for r in ranked], f"search:{args.query.lower()}", args.limit, args.cursor)

    def get_issue(self, issue_key: str) -> dict:
        args = IssueArguments(issue_key=issue_key)
        record = self.current(args.issue_key)
        if record is not None and len(json.dumps(record).encode()) > MAX_RECORD_BYTES:
            raise ValueError("Source record exceeds the permitted size")
        # Missing and inaccessible records deliberately have identical envelopes.
        return {"item": record, "found": record is not None}

    def get_thread(self, channel_id: str, thread_ts: str, limit: int = 10, cursor: str | None = None) -> dict:
        args = ThreadArguments(channel_id=channel_id, thread_ts=thread_ts, limit=limit, cursor=cursor)
        records = [r for r in self.records() if self.eligible(r) and r["channel_id"] == args.channel_id and r["thread_ts"] == args.thread_ts]
        records.sort(key=lambda r: r["ts"])
        return self._page(records, f"thread:{args.channel_id}:{args.thread_ts}", args.limit, args.cursor, "messages")
