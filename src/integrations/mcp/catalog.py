"""Reviewed tool schemas; discovery can never expand these permissions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Source = Literal["jira", "slack"]


class StrictArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SearchArguments(StrictArguments):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=10, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=2048)

    @field_validator("query")
    @classmethod
    def plain_query(cls, value: str) -> str:
        value = value.strip()
        if not value or re.search(r"[<>=:{}\[\]]|\b(?:AND|OR|NOT|ORDER BY)\b", value):
            raise ValueError("Only plain keywords and exact issue keys are supported")
        return value


class IssueArguments(StrictArguments):
    issue_key: str = Field(pattern=r"^[A-Z][A-Z0-9]{1,15}-[1-9][0-9]{0,9}$")


class ThreadArguments(StrictArguments):
    channel_id: str = Field(pattern=r"^C_[A-Z0-9_]{1,40}$")
    thread_ts: str = Field(pattern=r"^[0-9]{10}\.[0-9]{6}$")
    limit: int = Field(default=10, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=2048)


TOOL_ARGUMENTS: dict[str, dict[str, type[StrictArguments]]] = {
    "jira": {"search_issues": SearchArguments, "get_issue": IssueArguments},
    "slack": {"search_messages": SearchArguments, "get_thread": ThreadArguments},
}

APPROVED_DESCRIPTIONS = {
    "search_issues": "Search company-wide Jira issues by plain keywords or exact key.",
    "get_issue": "Read one company-wide Jira issue by its exact key.",
    "search_messages": "Search company-wide Slack messages by plain keywords or exact issue key.",
    "get_thread": "Read a bounded page of a company-wide Slack thread.",
}


class ToolDenied(ValueError):
    pass


@dataclass(frozen=True)
class AuthorizedCall:
    source: Source
    tool: str
    arguments: StrictArguments


def authorize_tool_call(source: Source, tool: str, arguments: dict) -> AuthorizedCall:
    model = TOOL_ARGUMENTS.get(source, {}).get(tool)
    if model is None:
        raise ToolDenied("Tool is not approved for this source")
    try:
        validated = model.model_validate(arguments)
    except (ValueError, TypeError) as exc:
        raise ToolDenied("Tool arguments did not match the approved schema") from exc
    return AuthorizedCall(source, tool, validated)


def _shape(schema: dict) -> dict:
    """Compare validation semantics while ignoring human-facing annotations."""
    return {
        key: [_shape(item) if isinstance(item, dict) else item for item in value]
        if isinstance(value, list)
        else _shape(value) if isinstance(value, dict)
        else value
        for key, value in schema.items()
        if key not in {"title", "description", "default", "$schema", "additionalProperties"}
    }


def validate_catalog(source: Source, discovered: list) -> dict[str, str]:
    result = {}
    for tool in discovered:
        model = TOOL_ARGUMENTS[source].get(tool.name)
        if model is None:
            continue
        expected = model.model_json_schema()
        actual = tool.inputSchema
        if (_shape(actual.get("properties", {})) != _shape(expected["properties"])
                or set(actual.get("required", [])) != set(expected.get("required", []))):
            raise ToolDenied("The source tool catalog changed incompatibly")
        result[tool.name] = APPROVED_DESCRIPTIONS[tool.name]
    if set(result) != set(TOOL_ARGUMENTS[source]):
        raise ToolDenied("The source is missing an approved tool")
    return result
