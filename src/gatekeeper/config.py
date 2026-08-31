"""Runtime configuration, loaded from the environment or a local .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GK_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Two DSNs on purpose. Migrations and the ingestion (admin) plane connect as the
    # table owner; the query (data) plane connects as a NOSUPERUSER role that cannot
    # bypass row-level security. See docs/adr/0002.
    database_owner_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5433/gatekeeper"
    database_url: str = (
        "postgresql+asyncpg://gatekeeper_app:gatekeeper_app@localhost:5433/gatekeeper"
    )

    redis_url: str = "redis://localhost:6380/0"

    s3_endpoint_url: str = "http://localhost:9002"
    s3_access_key: str = "gatekeeper"
    s3_secret_key: str = "gatekeeper"
    s3_bucket: str = "gatekeeper-raw"

    # --- authentication -----------------------------------------------
    # "dev" verifies HS256 tokens signed with a local secret, so the whole system runs
    # with no identity provider. "oidc" verifies RS256 against a JWKS endpoint.
    auth_mode: str = "dev"
    auth_dev_secret: str = "gatekeeper-dev-secret-do-not-use-in-production"
    allow_insecure_dev_auth: bool = False
    oidc_jwks_url: str | None = None
    oidc_issuer: str | None = None
    oidc_audience: str = "gatekeeper"
    # Which claim carries the principal handle. Keycloak uses `preferred_username`;
    # `sub` is an opaque uuid that will not match `principals.external_id`.
    oidc_subject_claim: str = "preferred_username"

    # The MCP server binds to exactly one principal for its lifetime; see
    # gatekeeper.apps.mcp.server for why that is the right model there and not a shortcut.
    mcp_principal: str | None = None

    embedding_backend: str = "local"
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None

    log_level: str = "INFO"

    # Tracing is off unless this is set. See `gatekeeper.core.telemetry` for why spans
    # carry shapes and never contents.
    otel_endpoint: str | None = None

    corpus_dir: Path = Field(default=REPO_ROOT / "corpus" / "sources")
    acl_rules_path: Path = Field(default=REPO_ROOT / "corpus" / "acl_rules.yaml")

    @property
    def sync_owner_url(self) -> str:
        """psycopg2 form of the owner DSN, for Alembic."""
        return self.database_owner_url.replace("+asyncpg", "")


@lru_cache
def get_settings() -> Settings:
    return Settings()
