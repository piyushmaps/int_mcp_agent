"""Run the local fixture MCP servers with real SDK stdio or Streamable HTTP.

Examples:
    python -m src.integrations.mcp.server jira
    python -m src.integrations.mcp.server slack --transport streamable-http
"""

from __future__ import annotations

import argparse
import hmac
import logging
import os
from typing import Annotated, Any

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .repository import FixtureRepository

Query = Annotated[str, Field(min_length=1, max_length=1000)]
Limit = Annotated[int, Field(ge=1, le=50)]
Cursor = Annotated[str | None, Field(max_length=2048)]
IssueKey = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9]{1,15}-[1-9][0-9]{0,9}$")]
Channel = Annotated[str, Field(pattern=r"^C_[A-Z0-9_]{1,40}$")]
Thread = Annotated[str, Field(pattern=r"^[0-9]{10}\.[0-9]{6}$")]


class LocalFixtureTokenVerifier:
    """Local test authentication only. This is deliberately not enterprise OAuth."""
    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token, os.environ.get("MCP_TOKEN", "local-mcp-token")):
            return None
        return AccessToken(token=token, client_id="local-fixture-host", scopes=["company-wide"])


def create_server(source: str, port: int | None = None) -> FastMCP:
    if os.environ.get("APP_ENV", "development").lower() in {"production", "prod"}:
        raise RuntimeError("Synthetic fixture servers cannot run in production")
    repository = FixtureRepository(source)
    port = port or (8011 if source == "jira" else 8012)
    server = FastMCP(f"Company-wide fixture {source}", host="127.0.0.1",
                     port=port, json_response=True, stateless_http=True, log_level="CRITICAL",
                     token_verifier=LocalFixtureTokenVerifier(),
                     auth=AuthSettings(issuer_url=f"http://127.0.0.1:{port}",
                                       resource_server_url=f"http://127.0.0.1:{port}/mcp",
                                       required_scopes=["company-wide"]))
    if source == "jira":
        @server.tool()
        def search_issues(query: Query, limit: Limit = 10, cursor: Cursor = None) -> dict[str, Any]:
            """Search company-wide Jira issues using plain keywords or an exact key."""
            return repository.search(query, limit, cursor)

        @server.tool()
        def get_issue(issue_key: IssueKey) -> dict[str, Any]:
            """Read one company-wide Jira issue, if available."""
            return repository.get_issue(issue_key)
    else:
        @server.tool()
        def search_messages(query: Query, limit: Limit = 10, cursor: Cursor = None) -> dict[str, Any]:
            """Search company-wide Slack messages using plain keywords or an exact key."""
            return repository.search(query, limit, cursor)

        @server.tool()
        def get_thread(channel_id: Channel, thread_ts: Thread, limit: Limit = 10, cursor: Cursor = None) -> dict[str, Any]:
            """Read an ordered, bounded page of a company-wide Slack thread."""
            return repository.get_thread(channel_id, thread_ts, limit, cursor)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=["jira", "slack"])
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    # Do not log SDK payloads or handler exception details.
    logging.disable(logging.CRITICAL)
    create_server(args.source, args.port).run(transport=args.transport)


if __name__ == "__main__":
    main()
