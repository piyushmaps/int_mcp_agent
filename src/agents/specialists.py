"""Bounded observe/decide/read loops with an optional approved model decision hook.

The default deterministic decision policy makes the local demo reproducible. A
model hook can choose the same reviewed reads, but code always owns permissions,
deadline, source scope, result screening and the shared request budget.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.contracts import Evidence, SpecialistResult
from src.integrations.mcp.budget import BudgetExhausted, CallBudget
from src.integrations.mcp.catalog import Source, ToolDenied
from src.integrations.mcp.client import MCPSourceClient, SourceUnavailable
from src.integrations.mcp.screening import ResultRejected
from src.policy.safety import sanitize_text


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["call", "finish"]
    tool: str | None = Field(default=None, max_length=80)
    arguments: dict = Field(default_factory=dict)


DecisionHook = Callable[[str, str, list[Evidence], dict[str, str]], Awaitable[dict]]


def _local_decision(source: Source, query: str, evidence: list[Evidence], calls: list[Decision],
                    last_observation) -> Decision:
    if not calls:
        keys = re.findall(r"\b[A-Z][A-Z0-9]{1,15}-[1-9][0-9]{0,9}\b", query.upper())
        if source == "jira" and keys:
            return Decision(action="call", tool="get_issue", arguments={"issue_key": keys[0]})
        # A documented plain-keyword subset; no vendor JQL or Slack operators.
        words = re.findall(r"[A-Za-z0-9_-]+", query)
        normalized = " ".join(keys or words)[:1000] or "unmatched"
        return Decision(action="call", tool="search_issues" if source == "jira" else "search_messages",
                        arguments={"query": normalized, "limit": 5})
    if source == "jira" and calls[-1].tool == "search_issues" and evidence:
        return Decision(action="call", tool="get_issue", arguments={"issue_key": evidence[0].source_id})
    if source == "slack" and calls[-1].tool == "search_messages" and evidence:
        metadata = evidence[0].metadata
        return Decision(action="call", tool="get_thread", arguments={
            "channel_id": metadata["channel_id"], "thread_ts": metadata["thread_ts"], "limit": 10})
    if last_observation is not None and last_observation.next_cursor:
        arguments = dict(calls[-1].arguments, cursor=last_observation.next_cursor)
        return Decision(action="call", tool=calls[-1].tool, arguments=arguments)
    return Decision(action="finish")


async def run_specialist(task_id: str, source: Source, query: str, client: MCPSourceClient,
                         budget: CallBudget, decision_hook: DecisionHook | None = None,
                         max_steps: int = 4) -> SpecialistResult:
    if source != client.source or not 1 <= max_steps <= 8:
        return SpecialistResult(task_id=task_id, source=source, status="denied",
                                limitations=["The source task did not match its approved reader."])
    collected: dict[str, Evidence] = {}
    limitations: list[str] = []
    calls: list[Decision] = []
    observation = None
    tool_calls = 0
    status = "empty"
    try:
        query = sanitize_text(query)[:1000]
        for _ in range(max_steps):
            if budget.remaining_seconds() <= 0:
                raise TimeoutError()
            if decision_hook is None:
                proposal = _local_decision(source, query, list(collected.values()), calls, observation)
            else:
                async with asyncio.timeout(min(10.0, budget.remaining_seconds())):
                    raw_decision = await decision_hook(source, query, list(collected.values()), dict(client.tools))
                proposal = Decision.model_validate(raw_decision)
            if proposal.action == "finish":
                status = "success" if collected else "empty"
                break
            if proposal.tool is None:
                raise ToolDenied("Invalid tool decision")
            # Prevent loops that repeat an identical read without learning anything.
            if any(proposal == previous for previous in calls):
                limitations.append("The source reader stopped a repeated read.")
                status = "partial" if collected else "failed"
                break
            calls.append(proposal)
            before = client.attempts
            try:
                observation = await client.invoke_read_tool(proposal.tool, proposal.arguments, budget)
            finally:
                # A reservation is counted even when the read failed or timed out.
                # The branch invokes serially; aggregate spending stays in budget.
                tool_calls += client.attempts - before
            for item in observation.evidence:
                if len(collected) >= 20 and item.id not in collected:
                    limitations.append("The source evidence limit was reached.")
                    break
                collected[item.id] = item
            limitations.extend(observation.limitations)
            status = "success" if collected else "empty"
        else:
            limitations.append("The source reader reached its bounded step limit.")
            status = "partial" if collected else "budget_exhausted"
    except BudgetExhausted:
        status = "partial" if collected else "budget_exhausted"
        limitations.append("The shared request tool budget was exhausted.")
    except (TimeoutError, asyncio.TimeoutError):
        status = "partial" if collected else "timed_out"
        limitations.append(f"The {source.title()} source could not be checked within the request deadline.")
    except ToolDenied:
        status = "partial" if collected else "denied"
        limitations.append("A source read was denied by the approved tool policy.")
    except (ResultRejected, SourceUnavailable):
        status = "partial" if collected else "failed"
        limitations.append(f"The {source.title()} source returned an unavailable or invalid result.")
    except Exception:
        # Neither provider exception prose nor malformed model/source text is public.
        status = "partial" if collected else "failed"
        limitations.append(f"The {source.title()} source could not complete this search.")
    if limitations and status == "success":
        status = "partial"
    return SpecialistResult(task_id=task_id, source=source, status=status,
                            evidence=list(collected.values()), limitations=list(dict.fromkeys(limitations)),
                            tool_calls=tool_calls)
