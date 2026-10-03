"""SDK lifecycle and authorized reads for a fixed, local source mapping."""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import AsyncExitStack
from datetime import timedelta
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from src.contracts import Evidence

from .budget import CallBudget
from .catalog import TOOL_ARGUMENTS, Source, ToolDenied, authorize_tool_call, validate_catalog
from .repository import FIXTURE_DIRECTORY, FixtureRepository
from .screening import SafeObservation, screen_tool_result


class SourceUnavailable(RuntimeError):
    pass


class MCPSourceClient:
    """A credential-separated handle; enter/exit in the same async task.

    The default stdio transport starts a separate SDK server process. HTTP mode
    connects only to configured loopback endpoints, never a model-proposed URL.
    Raw payloads and credentials never leave this adapter.
    """

    def __init__(self, source: Source, transport: str = "stdio", url: str | None = None,
                 token: str = "local-mcp-token", timeout_seconds: float = 10.0):
        if source not in TOOL_ARGUMENTS or transport not in {"stdio", "http"}:
            raise ValueError("Unsupported MCP source or transport")
        self.source = source
        self.transport = transport
        self.url = url or f"http://127.0.0.1:{8011 if source == 'jira' else 8012}/mcp"
        parts = urlsplit(self.url)
        if (parts.scheme != "http" or parts.hostname not in {"localhost", "127.0.0.1", "::1"}
                or parts.username or parts.password or parts.path != "/mcp" or parts.query or parts.fragment):
            raise ValueError("Only configured local fixture MCP endpoints are permitted")
        self._token = token
        self.timeout_seconds = timeout_seconds
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self.tools: dict[str, str] = {}
        self.attempts = 0
        self.repository = FixtureRepository(source)

    async def __aenter__(self) -> "MCPSourceClient":
        self._stack = AsyncExitStack()
        try:
            if self.transport == "stdio":
                params = StdioServerParameters(
                    command=sys.executable, args=["-m", "src.integrations.mcp.server", self.source],
                    cwd=str(FIXTURE_DIRECTORY.parent.parent),
                    env={"PYTHONUNBUFFERED": "1", "APP_ENV": os.environ.get("APP_ENV", "development"), "MCP_TOKEN": self._token},
                )
                sink = self._stack.enter_context(open(os.devnull, "w"))
                read, write = await self._stack.enter_async_context(stdio_client(params, errlog=sink))
            else:
                read, write, _ = await self._stack.enter_async_context(streamablehttp_client(
                    self.url, headers={"Authorization": f"Bearer {self._token}"},
                    timeout=timedelta(seconds=self.timeout_seconds),
                    sse_read_timeout=timedelta(seconds=self.timeout_seconds),
                ))
            self._session = await self._stack.enter_async_context(ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=self.timeout_seconds)))
            await self._session.initialize()
            discovered = []
            cursor = None
            # Catalog paging is bounded separately from source-result paging.
            for _ in range(4):
                catalog = await self._session.list_tools(cursor=cursor)
                discovered.extend(catalog.tools)
                cursor = catalog.nextCursor
                if cursor is None:
                    break
            else:
                raise ToolDenied("The source tool catalog exceeded discovery limits")
            self.tools = validate_catalog(self.source, discovered)
            return self
        except BaseException as error:
            try:
                await self._stack.aclose()
            except Exception:
                # Some SDK transports report a second closed-stream failure while
                # unwinding initialization. Preserve the safe primary outcome.
                pass
            self._stack = None
            self._session = None
            if isinstance(error, ToolDenied):
                raise
            if isinstance(error, asyncio.CancelledError) and asyncio.current_task().cancelling():
                # Preserve caller/deadline cancellation. A transport task-group
                # failure may also cancel initialization, but its own cancellation
                # is cleared when the SDK group unwinds above.
                raise
            raise SourceUnavailable("The source connection could not be established") from None

    async def __aexit__(self, exc_type, exc, tb):
        if self._stack is not None:
            await self._stack.__aexit__(exc_type, exc, tb)
        self._stack = None
        self._session = None
        self.tools = {}

    async def invoke_read_tool(self, tool: str, arguments: dict, budget: CallBudget) -> SafeObservation:
        call = authorize_tool_call(self.source, tool, arguments)
        if self._session is None or tool not in self.tools:
            raise SourceUnavailable("Source connection is unavailable")
        timeout = min(self.timeout_seconds, budget.remaining_seconds())
        if timeout <= 0:
            raise TimeoutError("Request deadline expired")
        async with asyncio.timeout(timeout):
            async with budget.semaphore:
                await budget.reserve()
                self.attempts += 1
                raw = await self._session.call_tool(call.tool, call.arguments.model_dump())
                # No graph node, tracing hook or caller ever receives this raw reply.
                return screen_tool_result(self.source, call.tool, raw, self.repository)

    async def is_current(self, evidence: Evidence) -> bool:
        if evidence.source != self.source:
            return False
        current = self.repository.current(evidence.source_id)
        return bool(current and current["revision"] == evidence.version
                    and current["eligibility"]["policy_version"] == evidence.eligibility_version)


# Explicit application-facing wrappers used by scripts and tests.
def open_connection(source: Source, **kwargs) -> MCPSourceClient:
    return MCPSourceClient(source, **kwargs)
