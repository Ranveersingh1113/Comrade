"""Typed application config, loaded and validated from .env at import time."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    # Direct Postgres connections (RLS-enforced; never service_role).
    comrade_db_url_admin: str       # tests only — seeds/cleans as table owner
    comrade_agent_db_url: str       # agent: reads + proposes + private nudges
    comrade_executor_db_url: str    # executes approved consent actions only
    comrade_pipeline_db_url: str    # document parser + memory compiler
    # PostgREST-style authenticator for user_session (SET ROLE authenticated).
    # Empty -> falls back to the admin URL (dev only; production must set it).
    comrade_authenticator_db_url: str = ""

    # Filled in as those features are built.
    gemini_api_key: str = ""
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_secret_key: str = ""
    github_pat: str = ""

    # HTTP surface. The JWT secret verifies Supabase-issued user tokens; without
    # it the API refuses to authenticate anyone rather than trusting the caller.
    supabase_jwt_secret: str = ""
    # Comma-separated browser origins allowed to call the API.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
