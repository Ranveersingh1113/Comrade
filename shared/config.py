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
    # Shared secret GitHub signs webhook deliveries with. EMPTY REFUSES EVERY
    # DELIVERY — server/webhooks.py fails closed rather than accepting unsigned
    # input, so a deployment that forgets this variable ingests nothing instead
    # of ingesting anything (findings §16.6).
    github_webhook_secret: str = ""

    # HTTP surface. The JWT secret verifies Supabase-issued user tokens; without
    # it the API refuses to authenticate anyone rather than trusting the caller.
    supabase_jwt_secret: str = ""
    # Comma-separated browser origins allowed to call the API.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # Per-team hourly cap on agent turns — the lid on LLM spend and the
    # simplest abuse brake. 0 disables the cap entirely.
    agent_turns_per_hour: int = 60

    # Where each team's checked-out repository lives. Deliberately outside
    # Comrade's own tree — shared/workspace.py refuses a value that overlaps
    # it, because team checkouts inside our repo would be kept out of git and
    # out of the agent's reach only by a .gitignore line.
    comrade_workspaces_root: str = "~/.comrade/workspaces"
    # Per-TURN cap on LLM calls (ADK RunConfig.max_llm_calls). ADK's own
    # default is 500; a Comrade turn is one plan + a handful of tool calls, so
    # 20 is generous headroom that still stops a tool loop from spending the
    # team's budget on one question.
    agent_max_llm_calls: int = 20
    # How many prior messages of the thread are replayed into the model's
    # context. Unbounded history is an unbounded bill; ~10 exchanges is enough
    # for "are you sure?" to mean something.
    agent_history_turns: int = 20

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
