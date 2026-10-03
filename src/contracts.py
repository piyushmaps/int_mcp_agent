"""Shared, validated envelopes. Raw provider records never enter workflow state."""
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)

class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Document(Contract):
    id: str
    source: str
    title: str
    body: str
    url: str = ""
    updated_at: datetime | None = None
    revision: str = "1"
    company_wide: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

class Evidence(Contract):
    id: str
    source: str
    source_id: str
    title: str
    text: str
    url: str = ""
    updated_at: datetime | None = None
    version: str = "1"
    location: str = ""
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    retrieved_at: datetime = Field(default_factory=utcnow)
    eligibility_version: str = "1"
    redaction_version: str = "1"

Source = Literal["jira", "slack", "documents"]
BranchStatus = Literal["success", "empty", "partial", "failed", "denied", "timed_out", "budget_exhausted", "skipped"]

class SpecialistResult(Contract):
    task_id: str
    source: Source
    status: BranchStatus
    evidence: list[Evidence] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    tool_calls: int = 0

class SearchTask(Contract):
    task_id: str
    source: Source
    query: str = Field(min_length=1, max_length=8000)
    needed_facts: list[str] = Field(default_factory=list, max_length=6)
    result_limit: int = Field(default=5, ge=1, le=10)

class SearchPlan(Contract):
    tasks: list[SearchTask] = Field(min_length=1, max_length=6)
    required_sources: list[Source] = Field(default_factory=list, max_length=3)
    rationale: str = Field(default="Search relevant approved sources.", max_length=300)

class Actor(Contract):
    id: str
    admin: bool = False

class RequestContext(Contract):
    request_id: str
    actor_ref: str
    session_ref: str
    policy_version: str = "1"
    deadline_at: datetime

class Claim(Contract):
    text: str = Field(max_length=2500)
    evidence_ids: list[str] = Field(min_length=1, max_length=5)
    supporting_quotes: list[str] = Field(default_factory=list, max_length=5)

class AnswerDraft(Contract):
    claims: list[Claim] = Field(default_factory=list, max_length=10)
    limitations: list[str] = Field(default_factory=list, max_length=10)

class Citation(Contract):
    id: str
    evidence_id: str
    source: str
    source_id: str
    title: str
    url: str
    location: str = ""
    updated_at: datetime | None = None
    version: str = "1"

class AnswerResult(Contract):
    request_id: str
    session_id: str
    status: Literal["answered", "partial", "insufficient_evidence", "blocked", "unavailable", "cancelled"]
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    selected_sources: list[str] = Field(default_factory=list)
    source_status: dict[str, str] = Field(default_factory=dict)
    routing_rationale: str = ""
    sub_queries: list[str] = Field(default_factory=list)
    as_of: datetime = Field(default_factory=utcnow)
    policy_version: str = "1"
    duration_ms: int = 0

class AskRequest(Contract):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, max_length=8000)
    session_id: str | None = Field(default=None, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")

    @field_validator("question")
    @classmethod
    def trim_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Question must not be blank")
        return value

