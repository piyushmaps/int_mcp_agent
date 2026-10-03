"""All runtime choices are explicit. Defaults are local synthetic data only."""
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ROOT / ".env"), extra="ignore")
    app_env: Literal["development", "production", "test"] = "development"
    model_mode: Literal["demo", "openai"] = "demo"
    embedding_mode: Literal["hash", "openai"] = "hash"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=1536, ge=32, le=3072)
    vector_backend: Literal["sqlite", "faiss", "opensearch"] = "sqlite"
    opensearch_endpoint: str = ""
    opensearch_index: str = "engineering-atlas-v1"
    opensearch_username: str = ""
    opensearch_password: SecretStr = SecretStr("")
    approved_remote_indexing: bool = False
    data_dir: Path = ROOT / "data" / "runtime"
    demo_token: SecretStr = SecretStr("local-demo-token")
    auth_mode: Literal["demo", "jwt"] = "demo"
    jwt_issuer: str = ""
    jwt_audience: str = ""
    jwt_jwks_url: str = ""
    jwt_required_group: str = "engineering-atlas"
    jwt_admin_group: str = "engineering-atlas-admin"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4.1-mini"
    approved_model_processing: bool = False
    model_max_input_chars: int = Field(default=32000, ge=1000, le=100000)
    model_max_output_tokens: int = Field(default=2000, ge=100, le=8000)
    model_total_tokens: int = Field(default=24000, ge=1000, le=100000)
    request_timeout: float = Field(default=30.0, ge=1, le=120)
    max_concurrent_requests: int = Field(default=12, ge=1, le=100)
    max_user_requests: int = Field(default=2, ge=1, le=10)
    max_tool_calls: int = Field(default=8, ge=1, le=20)
    session_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    session_max_turns: int = Field(default=6, ge=1, le=20)
    max_upload_bytes: int = Field(default=10000000, ge=1024, le=50000000)
    bootstrap_demo: bool = True
    mcp_transport: Literal["stdio", "http"] = "stdio"
    jira_mcp_url: str = "http://127.0.0.1:8011/mcp"
    slack_mcp_url: str = "http://127.0.0.1:8012/mcp"
    mcp_token: SecretStr = SecretStr("local-mcp-token")
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "[::1]", "testserver"]

    @model_validator(mode="after")
    def valid_profile(self):
        if self.auth_mode == "jwt" and not all([self.jwt_issuer, self.jwt_audience, self.jwt_jwks_url.startswith("https://")]):
            raise ValueError("JWT mode requires issuer, audience and HTTPS JWKS URL")
        if (self.model_mode == "openai" or self.embedding_mode == "openai") and (not self.openai_api_key or not self.approved_model_processing):
            raise ValueError("OpenAI mode requires an API key and approved_model_processing=true")
        if self.app_env == "production":
            # This distribution intentionally cannot masquerade as an approved enterprise deployment.
            raise ValueError("Production release gates remain open; see docs/implementation-status.md")
        if self.vector_backend == "opensearch" and not self.approved_remote_indexing:
            raise ValueError("OpenSearch requires approved_remote_indexing=true")
        return self

