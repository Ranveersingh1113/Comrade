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

    # Filled in as those features are built.
    gemini_api_key: str = ""
    openai_api_key: str = ""
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_secret_key: str = ""
    github_pat: str = ""


settings = Settings()
