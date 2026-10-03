"""Validate, authorize and sanitize raw source replies inside one private boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.contracts import Evidence
from src.policy.safety import UnsafeContentError, safe_url, sanitize_text

from .repository import MAX_RESPONSE_BYTES, FixtureRepository


class ResultRejected(ValueError):
    pass


class Eligibility(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audience: Literal["company_wide", "restricted", "unknown"]
    policy_version: str = Field(min_length=1, max_length=100)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=1, max_length=100)
    updated_at: datetime
    url: str = Field(max_length=2000)
    eligibility: Eligibility
    deleted: bool = False


class JiraRecord(Record):
    issue_key: str = Field(pattern=r"^[A-Z][A-Z0-9]{1,15}-[1-9][0-9]{0,9}$")
    title: str = Field(min_length=1, max_length=1000)
    description: str = Field(max_length=12000)
    status: str = Field(max_length=100)
    team: str = Field(max_length=200)


class SlackRecord(Record):
    message_id: str = Field(pattern=r"^C_[A-Z0-9_]{1,40}:[0-9]{10}\.[0-9]{6}$")
    channel_id: str = Field(pattern=r"^C_[A-Z0-9_]{1,40}$")
    channel_name: str = Field(max_length=200)
    thread_ts: str = Field(pattern=r"^[0-9]{10}\.[0-9]{6}$")
    ts: str = Field(pattern=r"^[0-9]{10}\.[0-9]{6}$")
    text: str = Field(max_length=12000)


@dataclass
class SafeObservation:
    evidence: list[Evidence] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    next_cursor: str | None = None
    truncated: bool = False


def screen_tool_result(source: str, tool: str, raw_result, repository: FixtureRepository) -> SafeObservation:
    if raw_result.isError:
        raise ResultRejected("The source tool could not complete the read")
    if any(block.type != "text" for block in raw_result.content):
        raise ResultRejected("The source returned an unsupported content type")
    if len(raw_result.model_dump_json().encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ResultRejected("The source response exceeded the permitted size")
    payload = raw_result.structuredContent
    if not isinstance(payload, dict):
        raise ResultRejected("The source returned an invalid result envelope")
    if tool == "get_issue":
        if set(payload) != {"item", "found"} or type(payload["found"]) is not bool:
            raise ResultRejected("The source returned an invalid result envelope")
        if payload["found"] != (payload["item"] is not None):
            raise ResultRejected("The source returned inconsistent availability")
        records = [] if payload["item"] is None else [payload["item"]]
    else:
        field_name = "messages" if tool == "get_thread" else "items"
        if set(payload) != {field_name, "next_cursor", "truncated", "observed_at"}:
            raise ResultRejected("The source returned an invalid result envelope")
        records = payload[field_name]
        cursor = payload["next_cursor"]
        if (not isinstance(records, list) or len(records) > 50 or type(payload["truncated"]) is not bool
                or (cursor is not None and (not isinstance(cursor, str) or len(cursor) > 2048))):
            raise ResultRejected("The source returned an invalid result page")
        try:
            datetime.fromisoformat(payload["observed_at"])
        except (ValueError, TypeError):
            raise ResultRejected("The source returned an invalid observation time") from None
        if bool(cursor) != payload["truncated"]:
            raise ResultRejected("The source returned inconsistent pagination")
    result = SafeObservation(next_cursor=payload.get("next_cursor"), truncated=payload.get("truncated", False))
    model = JiraRecord if source == "jira" else SlackRecord
    for raw in records:
        try:
            record = model.model_validate(raw)
            source_id = record.issue_key if source == "jira" else record.message_id
            trusted = repository.current(source_id)
            if (trusted is None or record.deleted or record.eligibility.audience != "company_wide"
                    or trusted["revision"] != record.revision
                    or trusted["eligibility"]["policy_version"] != record.eligibility.policy_version
                    or trusted["url"] != record.url):
                raise ResultRejected("Eligibility or revision could not be verified")
            # Screen all string fields, including titles, metadata and links.
            strings = {key: sanitize_text(value) for key, value in record.model_dump().items() if isinstance(value, str)}
            metadata = {"audience": "company_wide", "policy_version": sanitize_text(record.eligibility.policy_version)}
            if source == "jira":
                title = strings["title"]
                text = f"Status: {strings['status']}. Team: {strings['team']}. {strings['description']}"
                location = record.issue_key
                metadata.update(status=strings["status"], team=strings["team"])
            else:
                title = f"#{strings['channel_name']}"
                text = strings["text"]
                location = f"Thread {record.thread_ts}, message {record.ts}"
                metadata.update(channel_id=record.channel_id, thread_ts=record.thread_ts, ts=record.ts)
            result.evidence.append(Evidence(
                id=f"{source}:{source_id}:{record.revision}", source=source, source_id=source_id,
                title=title, text=text, url=safe_url(record.url), updated_at=record.updated_at,
                version=record.revision, location=location, score=1.0, metadata=metadata,
                eligibility_version=record.eligibility.policy_version,
            ))
        except (ValueError, TypeError, UnsafeContentError):
            result.limitations.append("Some source content was withheld because it could not pass eligibility or safety checks.")
    result.limitations = list(dict.fromkeys(result.limitations))
    return result
