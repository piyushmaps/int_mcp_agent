"""Real protocol contracts plus defensive boundaries for synthetic sources."""

import asyncio
import copy
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from mcp.types import CallToolResult, Tool

from src.agents.specialists import run_specialist
from src.integrations.mcp.budget import BudgetExhausted, CallBudget
from src.integrations.mcp.catalog import TOOL_ARGUMENTS, ToolDenied, authorize_tool_call, validate_catalog
from src.integrations.mcp.client import MCPSourceClient
from src.integrations.mcp.repository import FixtureRepository
from src.integrations.mcp.screening import ResultRejected, screen_tool_result

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_real_stdio_jira_discovery_read_and_safety():
    async with MCPSourceClient("jira") as client:
        assert set(client.tools) == {"search_issues", "get_issue"}
        result = await run_specialist("jira-task", "jira", "Why is PAY-123 delayed?", client, CallBudget())
        assert result.status == "success"
        assert result.tool_calls == 1
        assert result.evidence[0].source_id == "PAY-123"
        assert "idempotency" in result.evidence[0].text
        assert await client.is_current(result.evidence[0])
        for key in ("PAY-999", "PAY-777", "PAY-100000"):
            observation = await client.invoke_read_tool("get_issue", {"issue_key": key}, CallBudget())
            assert observation.evidence == []
            assert observation.limitations == []
        injection = await client.invoke_read_tool("get_issue", {"issue_key": "PAY-666"}, CallBudget())
        assert injection.evidence == []
        assert injection.limitations
        assert "reveal" not in " ".join(injection.limitations).lower()


@pytest.mark.asyncio
async def test_real_stdio_slack_search_reads_newer_correction():
    async with MCPSourceClient("slack") as client:
        result = await run_specialist("slack-task", "slack", "Why is PAY-123 delayed?", client, CallBudget())
        assert result.status == "success"
        assert result.tool_calls == 2
        assert len(result.evidence) == 3
        assert any("Correction" in e.text and "staging environment is healthy" in e.text for e in result.evidence)
        assert all(e.metadata["audience"] == "company_wide" for e in result.evidence)


@pytest.fixture(scope="module")
def http_server():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "src.integrations.mcp.server", "jira", "--transport", "streamable-http", "--port", str(port)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=dict(os.environ, APP_ENV="test", MCP_TOKEN="local-mcp-token"),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    endpoint = f"http://127.0.0.1:{port}/mcp"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if httpx.get(endpoint, timeout=0.5).status_code == 401:
                    break
            except httpx.TransportError:
                pass
            if process.poll() is not None:
                pytest.fail("Local HTTP MCP server exited before readiness")
            time.sleep(0.05)
        else:
            pytest.fail("Local HTTP MCP server did not become ready")
        yield endpoint
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.asyncio
async def test_real_streamable_http_protocol_and_auth(http_server):
    assert httpx.post(http_server, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
    async with MCPSourceClient("jira", transport="http", url=http_server) as client:
        observation = await client.invoke_read_tool("get_issue", {"issue_key": "PAY-123"}, CallBudget())
        assert observation.evidence[0].source_id == "PAY-123"


def test_pagination_is_stable_scope_bound_and_exact_reads_recheck_access():
    repository = FixtureRepository("jira")
    first = repository.search("Payments", limit=1)
    assert first["truncated"] and first["next_cursor"]
    second = repository.search("Payments", limit=1, cursor=first["next_cursor"])
    assert first["items"][0]["issue_key"] != second["items"][0]["issue_key"]
    with pytest.raises(ValueError, match="cursor"):
        repository.search("Operations", cursor=first["next_cursor"])
    with pytest.raises(ValueError, match="cursor"):
        repository.search("Payments", cursor="forged")
    assert repository.get_issue("PAY-999") == repository.get_issue("PAY-100000")
    assert repository.get_issue("PAY-777") == repository.get_issue("PAY-100000")


@pytest.mark.parametrize("tool,args", [
    ("delete_issue", {"issue_key": "PAY-123"}),
    ("get_issue", {"issue_key": "PAY-123", "allowed_scope": "all"}),
    ("get_issue", {"issue_key": "../credentials"}),
    ("search_issues", {"query": "project = PAY"}),
    ("search_issues", {"query": "Payments", "limit": 51}),
    ("search_issues", {"query": "Payments", "limit": True}),
])
def test_tool_gate_rejects_unapproved_names_and_arguments(tool, args):
    with pytest.raises(ToolDenied):
        authorize_tool_call("jira", tool, args)


def test_changed_catalog_cannot_expand_permissions():
    tools = [Tool(name=name, inputSchema=model.model_json_schema()) for name, model in TOOL_ARGUMENTS["jira"].items()]
    tools.append(Tool(name="delete_issue", inputSchema={"type": "object"}))
    assert set(validate_catalog("jira", tools)) == {"search_issues", "get_issue"}
    tools[0].inputSchema["required"].append("employee_id")
    with pytest.raises(ToolDenied):
        validate_catalog("jira", tools)


def test_private_screen_rejects_errors_forged_eligibility_and_unsafe_links():
    repository = FixtureRepository("jira")
    with pytest.raises(ResultRejected):
        screen_tool_result("jira", "get_issue", CallToolResult(isError=True, content=[]), repository)
    with pytest.raises(ResultRejected):
        screen_tool_result("jira", "get_issue", CallToolResult(content=[], structuredContent={"arbitrary": "raw"}), repository)
    raw = copy.deepcopy(repository.get_issue("PAY-123"))
    raw["item"]["url"] = "https://evil.example.com/steal"
    observation = screen_tool_result("jira", "get_issue", CallToolResult(content=[], structuredContent=raw), repository)
    assert not observation.evidence and observation.limitations
    raw["item"]["url"] = "https://jira.example.com/browse/PAY-123"
    raw["item"]["issue_key"] = "PAY-999"
    observation = screen_tool_result("jira", "get_issue", CallToolResult(content=[], structuredContent=raw), repository)
    assert not observation.evidence


@pytest.mark.asyncio
async def test_budget_reservation_is_atomic_across_parallel_branches():
    budget = CallBudget(max_calls=2)
    results = await asyncio.gather(*(budget.reserve() for _ in range(8)), return_exceptions=True)
    assert budget.used == 2
    assert sum(isinstance(item, BudgetExhausted) for item in results) == 6


@pytest.mark.asyncio
async def test_specialist_model_cannot_escalate_or_loop():
    async def malicious_model(source, query, evidence, tools):
        return {"action": "call", "tool": "delete_issue", "arguments": {"issue_key": "PAY-123"}}

    async def looping_model(source, query, evidence, tools):
        return {"action": "call", "tool": "get_issue", "arguments": {"issue_key": "PAY-123"}}

    async with MCPSourceClient("jira") as client:
        budget = CallBudget()
        result = await run_specialist("bad-model", "jira", "PAY-123", client, budget, malicious_model)
        assert result.status == "denied" and budget.used == 0
        result = await run_specialist("loop-model", "jira", "PAY-123", client, budget, looping_model)
        assert result.status == "partial" and result.tool_calls == 1
        assert budget.used == 1


@pytest.mark.asyncio
async def test_timeout_keeps_attempt_charged_and_raw_details_private(monkeypatch):
    async with MCPSourceClient("jira") as client:
        client.timeout_seconds = 0.05
        async def slow_call(*args, **kwargs):
            await asyncio.sleep(1)
        monkeypatch.setattr(client._session, "call_tool", slow_call)
        budget = CallBudget()
        result = await run_specialist("timeout", "jira", "PAY-123", client, budget)
        assert result.status == "timed_out"
        assert result.tool_calls == budget.used == 1


@pytest.mark.asyncio
async def test_release_rechecks_revision_and_company_wide_eligibility(monkeypatch):
    async with MCPSourceClient("jira") as client:
        observation = await client.invoke_read_tool("get_issue", {"issue_key": "PAY-123"}, CallBudget())
        evidence = observation.evidence[0]
        current = client.repository.current("PAY-123")
        current["revision"] = "changed-after-read"
        monkeypatch.setattr(client.repository, "current", lambda source_id: current)
        assert not await client.is_current(evidence)
        monkeypatch.setattr(client.repository, "current", lambda source_id: None)
        assert not await client.is_current(evidence)
