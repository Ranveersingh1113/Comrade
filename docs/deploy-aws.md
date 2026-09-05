# Deploying Comrade on AWS — a pilot

Written 2026-09-05. One EC2 instance running the compose stack, plus a hosted
Supabase project. Aimed at a pilot team you can talk to, not a public signup —
read the Ceilings section of `operations.md` before inviting anyone.

**Why not Fargate.** `docs/superpowers/plans/2026-09-05-aws-fargate-sandbox.md`
is the right production answer for the sandbox and it is a build, not a deploy.
The code as it stands runs `repo_run` against a Docker daemon, and one EC2
instance has one. Fargate replaces the sandbox backend later without changing
anything else here.

**Why Supabase and not RDS.** The authorization model *is* Supabase: RLS
policies, `auth.uid()`, four login roles, and Realtime. RDS Postgres has none
of the auth layer, and running without it does not weaken the model, it deletes
it.

## Cost, on $100 of credits

| item | rate | month |
|---|---|---|
| EC2 `t3.medium` (2 vCPU, 4 GiB) | $0.0416/hr | ~$30 |
| 30 GB gp3 root volume | $0.08/GB | ~$2.40 |
| Elastic IP (while attached) | free | $0 |
| Supabase free tier | — | $0 |

≈ **$33/month, so about three months.** `t3.small` (2 GiB) is tempting and too
small: a sandbox run is capped at 512 MB (`agent/sandbox.MEMORY`) on top of
four Python processes and the Docker daemon.

Supabase's free tier pauses a project after a week of inactivity. For a pilot
that is fine; know that it is why the app went down.

---

## Steps

**you** marks a step only a person can do — creating accounts, entering
credentials, authorising OAuth.

### 1. Database

1. **you** — Create a Supabase project (free tier). From *Project Settings*:
   - `SUPABASE_URL`, `SUPABASE_ANON_KEY` (API)
   - `SUPABASE_SECRET_KEY` (service role)
   - `SUPABASE_JWT_SECRET` (JWT keys — legacy HS256 secret)
   - the Postgres connection string and its password
2. **you** — *Authentication → URL Configuration*: set the Site URL to
   `https://<your-host>` and add it to Redirect URLs. Sign-in silently fails to
   redirect otherwise, and the error surfaces nowhere useful.
3. Push the schema from your laptop:
   ```bash
   npx supabase link --project-ref <ref>
   npx supabase db push
   ```
4. **you** — Create the login roles. **`scripts/restore_local_roles.py` does not
   work here** — it shells out to `docker exec` against the local container.
   Run the SQL directly, with passwords you generate:
   ```bash
   psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 \
     -v agent_pwd="$(openssl rand -base64 24)" \
     -v executor_pwd="$(openssl rand -base64 24)" \
     -v pipeline_pwd="$(openssl rand -base64 24)" \
     -v control_pwd="$(openssl rand -base64 24)" \
     -v authenticator_pwd="$(openssl rand -base64 24)" \
     -f scripts/setup_local_roles.sql
   ```
   Keep each password — they go into the five `COMRADE_*_DB_URL` values, and
   nothing prints them again. The migrations create the *group* roles; this is
   what gives them LOGIN.

### 2. Keys

5. **you** — A Gemini API key. Every team's turns bill to it; see **Money** in
   `operations.md` and do not set the hourly caps to 0.
6. **you** — A GitHub App (Settings → Developer settings → GitHub Apps):
   - Callback URL `https://<host>/api/github/callback`
   - Webhook URL `https://<host>/api/webhooks/github`, with a secret you generate
   - Enable "Request user authorization (OAuth) during installation"
   - Repository permissions: Contents read/write, Pull requests read/write,
     Metadata read; subscribe to Push and Pull request events
   - Collect `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_APP_CLIENT_ID`,
     `GITHUB_APP_CLIENT_SECRET`, `GITHUB_WEBHOOK_SECRET`, and generate a private
     key (`GITHUB_APP_PRIVATE_KEY`).
   - **Leave `GITHUB_PAT` empty.** It is scoped to everything its owner can
     reach, and `pipeline/repo_sync.py` refuses it the moment a second team
     connects a repository.

### 3. The box

7. **you** — Launch EC2: Ubuntu 24.04 LTS, `t3.medium`, 30 GB gp3, a key pair
   you keep. Security group inbound: **22 from your IP only**, 80 and 443 from
   anywhere. Allocate an Elastic IP and associate it — the public IP changes on
   every stop/start otherwise, and the certificate is issued against a name.
8. **you** — Pick the hostname. With a domain, point an A record at the Elastic
   IP. Without one, use nip.io: for `13.51.7.20` the host is `13-51-7-20.nip.io`,
   and Let's Encrypt will issue for it.
9. On the box:
   ```bash
   sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
   sudo usermod -aG docker ubuntu && newgrp docker
   git clone <your repo> comrade && cd comrade
   docker build -f docker/sandbox.Dockerfile -t comrade-sandbox:latest .
   ```
   The sandbox image must exist **on this host** — the agent worker asks the
   host daemon to start it, and a missing image surfaces to the agent as "the
   image has not been built".

### 4. Configuration

10. **you** — Write `.env` on the box from `.env.example`. The ones that are not
    obvious:
    ```dotenv
    COMRADE_HOST=13-51-7-20.nip.io
    CORS_ORIGINS=https://13-51-7-20.nip.io
    COMRADE_WORKSPACES_ROOT=/workspaces

    # Supabase pooler, NOT 127.0.0.1:54322. Password-encode any / + = in the
    # generated passwords, or the URL parses wrong and every login fails.
    COMRADE_DB_URL_ADMIN=postgresql://postgres:<pw>@<host>:5432/postgres
    COMRADE_AGENT_DB_URL=postgresql://comrade_agent:<pw>@<host>:5432/postgres
    COMRADE_EXECUTOR_DB_URL=postgresql://comrade_executor:<pw>@<host>:5432/postgres
    COMRADE_PIPELINE_DB_URL=postgresql://comrade_pipeline:<pw>@<host>:5432/postgres
    COMRADE_CONTROL_DB_URL=postgresql://comrade_control:<pw>@<host>:5432/postgres
    COMRADE_AUTHENTICATOR_DB_URL=postgresql://comrade_authenticator:<pw>@<host>:5432/postgres

    # Baked into the JS bundle at BUILD time. Setting these on a running
    # container changes nothing.
    VITE_SUPABASE_URL=https://<ref>.supabase.co
    VITE_SUPABASE_ANON_KEY=<anon key>
    VITE_AGENT_API_URL=https://13-51-7-20.nip.io/api
    ```
    A deployment that hosts other people's code should also set
    `COMRADE_REQUIRE_LOCKFILE=true` — an unpinned install resolves to whatever
    the registry serves that day.

### 5. Up

11. ```bash
    docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
    curl https://<host>/api/ready
    ```
    Expect `{"status":"ready","checks":{...}}`. `not_ready` names the subsystem:
    `migrations: behind` means step 3 was skipped, `agent_queue` means the agent
    worker is not draining.

12. **you** — Open `https://<host>`, sign up, create a team, invite one person,
    connect a repository, and ask Comrade something in a thread. That is the
    first end-to-end run this stack has ever had; expect to find something.

---

## What to check first when it does not work

| symptom | cause |
|---|---|
| certificate never issues | port 80 blocked, or the hostname does not resolve to this box |
| every login fails | role passwords not URL-encoded, or step 4 skipped |
| sign-in redirects to localhost | Supabase Site URL still the default |
| `repo_run` fails with a mount error | `COMRADE_WORKSPACES_ROOT` and the host volume path disagree |
| agent turns accepted, nothing happens | agent worker down — `docker compose logs agent-worker` |
| GitHub connect returns to an error | callback URL missing the `/api` prefix |

## After the pilot boots

In this order, from `operations.md` Ceilings:

1. **Observability.** No correlation IDs, no counters, no alerting. Right now a
   production incident reaches you only if a member mentions it.
2. **Sandbox egress.** `run_setup` reaches the whole internet as root in the
   container. Opt-in per repository and holding no Comrade credentials, but
   enabling an environment means executing that repository's dependency graph.
3. **Fargate**, per the other plan, once the Docker-socket model stops being
   acceptable.
