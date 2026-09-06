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
    comrade_control_db_url: str = ""  # cross-team queue/sweep metadata only
    # PostgREST-style authenticator for user_session (SET ROLE authenticated).
    # Empty -> falls back to the admin URL (dev only; production must set it).
    comrade_authenticator_db_url: str = ""

    # Filled in as those features are built.
    gemini_api_key: str = ""
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_secret_key: str = ""
    # A GitHub App is how a team's repositories are reached. The App's private
    # key is the ONE long-lived secret: every repository credential is minted
    # from it on demand, scoped by GitHub to that installation, and held only
    # in memory (shared/github_app.py).
    github_app_id: str = ""
    github_app_private_key: str = ""      # PEM, escaped-newline PEM, or base64
    github_app_slug: str = ""             # for the https://github.com/apps/<slug> link
    # "Request user authorization (OAuth) during installation" on the App.
    # These verify that whoever finishes an install can actually ADMINISTER
    # the installation they are claiming — GitHub sends installation_id back
    # as an unauthenticated number in a URL, and installation ids are small
    # sequential integers. See server/github_connect.py.
    github_app_client_id: str = ""
    github_app_client_secret: str = ""

    # LOCAL SINGLE-TENANT ESCAPE HATCH, and nothing more.
    #
    # This used to be THE credential, returned by _token_for for every team
    # regardless of which team asked — so it was scoped to everything its
    # owner could reach, including other teams' private repositories. It is
    # kept only so a solo developer can run the harness before registering an
    # App, and pipeline/repo_sync.py refuses to use it the moment a second
    # team has connected a repository.
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

    # The SECOND dimension of the same budget, and the one that tracks cost.
    #
    # Counting turns treats "what's my status" and a turn that reads twenty
    # files as the same thing. A measured trivial turn costs ~5,100 input
    # tokens before the member types a word — system prompt, wiki index and 19
    # tool declarations — so 60 turns is somewhere between 300K and several
    # million depending entirely on what was asked. That is not a budget, it is
    # a turnstile.
    #
    # 500K is ~100 trivial turns, and it binds first on exactly the runs worth
    # bounding: a repository sweep that reads file after file.
    agent_tokens_per_hour: int = 500_000

    # What a turn is assumed to cost before it has run.
    #
    # A cap can only be enforced atomically against a number known at the time
    # the turn is admitted, and the real number does not exist until the turn
    # is over. So a turn reserves this and reconciles the truth when it
    # finishes (shared/usage.py).
    #
    # 6,000 from measurement: a trivial turn was ~5,100 input tokens before
    # the member typed a word. Too low and a burst of simultaneous turns can
    # overshoot the cap by the difference; too high and a team is refused work
    # it could have afforded. It is a reservation, not a charge — a cheap turn
    # gives the balance straight back.
    agent_tokens_estimate: int = 6_000

    # What a turn COSTS, as opposed to how many there were.
    #
    # Tokens are recorded unconditionally — they are a fact about what
    # happened. Cost is not: it depends on a price that changes, that differs
    # per deployment (free tier, committed use, a different model), and that
    # nobody should be guessing on someone else's behalf. Left at 0 these stay
    # null in agent_runs rather than becoming a confidently wrong number, and
    # `select sum(input_tokens)` still answers every question that matters.
    #
    # Set from the current published rate for whatever MODEL names, per
    # MILLION tokens.
    gemini_input_usd_per_mtok: float = 0.0
    gemini_output_usd_per_mtok: float = 0.0

    # Where each team's checked-out repository lives. Deliberately outside
    # Comrade's own tree — shared/workspace.py refuses a value that overlaps
    # it, because team checkouts inside our repo would be kept out of git and
    # out of the agent's reach only by a .gitignore line.
    comrade_workspaces_root: str = "~/.comrade/workspaces"
    # Total disk the checkouts may occupy. A full disk takes Postgres with it,
    # so this is an availability bound, not tidiness. Checkouts are always
    # re-clonable, which is what makes eviction safe.
    comrade_workspaces_max_gb: float = 20.0
    # Total across every dependency environment. A node_modules or a torch
    # install is gigabytes, and a handful of enabled repositories fills a disk
    # that Postgres is also on. Volumes are always rebuildable from the
    # manifest, which is what makes eviction safe here in the way it is for
    # checkouts.
    comrade_env_max_gb: float = 10.0
    # Refuse to build an environment from an unpinned manifest.
    #
    # OFF by default, and that is a judgement rather than laziness: an
    # unhashed requirements.txt is the norm in Python, so requiring a lockfile
    # would exclude most repositories that need this and the feature would
    # simply go unused. A deployment that hosts other people's code should turn
    # it on — an unpinned install resolves to whatever the registry serves
    # today, which is both irreproducible and the supply-chain surface.
    comrade_require_lockfile: bool = False
    # The image the team's own code runs in. One image for every team for now:
    # it is a knob a repository must not be able to turn, since "run my tests
    # in MY image" is just "run my code on your host" with extra steps. A
    # per-team value belongs in the teams table with an allowlist, not here.
    # The domain previews are served from. MUST NOT be the application's own
    # domain, and empty DISABLES previews entirely.
    #
    # 🔴 Previews used to answer on Comrade's hostname under /previews/<id>/.
    # A development server written by a model, running a team's unreviewed
    # code, on the same browser ORIGIN as the app — so its JavaScript could
    # read the member's Supabase session out of localStorage. Header stripping
    # at the proxy is irrelevant to that; the boundary a browser enforces is
    # the origin.
    #
    # Each process now answers on `<label>.<this domain>`, which is a different
    # site, so no Comrade cookie or token can travel there. Empty means "not
    # configured", and previews then fail closed with a message that says so
    # rather than silently falling back to the unsafe arrangement.
    # The container the preview proxy runs in — the API. Each preview gets its
    # OWN network, and this container is attached to each one so it can reach
    # them; nothing else is. Empty fails closed: without it there is no way to
    # give the proxy access without putting every preview on one shared
    # network, which is what this replaces.
    # Dependency setup is the ONE phase that needs the network, and it runs a
    # repository's own build hooks as root. It now runs on an internal network
    # with no route out, where the only path to a registry is this proxy.
    #
    # That is what makes the policy enforced rather than declared: with no
    # route, a direct IP, a DNS lookup, an IPv6 address, a redirect and a
    # metadata endpoint all fail for the same reason — there is nowhere to go
    # except through the proxy, which decides what it will fetch.
    #
    # BOTH EMPTY DISABLES DEPENDENCY SETUP. Failing closed is the point: the
    # previous behaviour was unrestricted egress, so "not configured" must mean
    # "no setup" and never "setup with the whole internet".
    comrade_setup_proxy_container: str = ""
    comrade_setup_proxy_url: str = ""
    comrade_preview_proxy_container: str = ""
    comrade_preview_domain: str = ""
    # How long a launch grant may be redeemed for. Single-use as well as short:
    # it appears in a URL, and URLs survive in history and screenshots.
    comrade_preview_grant_seconds: int = 60
    comrade_sandbox_image: str = "comrade-sandbox:latest"
    # Docker is only the local development backend. Production switches to a
    # server-owned ASCII Box key; this value never reaches browser code or a Box.
    comrade_sandbox_backend: str = "docker"
    comrade_box_api_key: str = ""
    comrade_box_max_archive_bytes: int = 50_000_000
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
