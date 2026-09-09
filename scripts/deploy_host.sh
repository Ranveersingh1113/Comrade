#!/usr/bin/env sh
# Deploy one reviewed Git commit on the single pilot host.
#
# 🔴 THE ORDER IS THE WHOLE FILE. This used to run `up -d --build` and then the
# migrations, which is backwards in the way that costs you the site: `up`
# activates the new code, so a migration that then fails leaves the running
# stack on new code against an old schema. The healthy stack was already gone
# before anything checked whether the release worked.
#
# Build a CANDIDATE, migrate, and only then activate. Every failure before the
# activation step leaves the previous containers running and untouched.
#
#   1. take the host lock      — two deploys must not interleave
#   2. fetch the exact SHA     — never a branch tip, which moves
#   3. build candidate images  — a broken build stops here
#   4. run migrations one-off  — with --no-deps, so this does not start the app
#   5. activate                — the first irreversible step
#   6. wait for readiness      — and fail loudly if it never comes
set -eu

[ "$#" -eq 1 ] || { echo "usage: $0 <commit>" >&2; exit 2; }
COMMIT="$1"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
WORKSPACES="${COMRADE_WORKSPACES_ROOT:-/workspaces}"
SANDBOX_UID=10001

# ---------------------------------------------------------------------------
# 1. One deploy at a time.
# ---------------------------------------------------------------------------
# Taken BEFORE the checkout: two overlapping deploys would otherwise fetch
# different commits into the same working tree, and the second would build
# whatever the first had checked out. Non-blocking, because a queued deploy
# behind a long migration is a deploy nobody is watching — the workflow
# serializes for us and a contended lock here means something is genuinely
# already running.
exec 9>"/tmp/comrade-deploy.lock"
flock -n 9 || { echo "another deploy holds the host lock" >&2; exit 3; }

# ---------------------------------------------------------------------------
# 2. The exact commit.
# ---------------------------------------------------------------------------
git fetch --depth=1 origin "$COMMIT"
git checkout --detach --force "$COMMIT"

# ---------------------------------------------------------------------------
# 3. Host paths the containers bind.
# ---------------------------------------------------------------------------
# Owned by the sandbox uid on the HOST. A Dockerfile `chown` sets ownership
# inside an image layer, and a bind mount replaces that layer wholesale — so
# image-layer ownership says nothing about a host-mounted folder. Without this
# the worker writes a thread's worktree as a user that cannot clean it up.
mkdir -p "$WORKSPACES"
chown "$SANDBOX_UID:$SANDBOX_UID" "$WORKSPACES"
owner="$(stat -c %u "$WORKSPACES")"
if [ "$owner" != "$SANDBOX_UID" ] && [ "$owner" != "999" ]; then
  echo "$WORKSPACES is owned by uid $owner, not $SANDBOX_UID" >&2
  exit 4
fi

# ---------------------------------------------------------------------------
# 3b. The group that can actually reach the daemon.
# ---------------------------------------------------------------------------
# 🔴 (fix.md, hackathon preflight.) Both workers mount /var/run/docker.sock and
# join `${COMRADE_DOCKER_GID:-999}`. 999 is a guess, and on the pilot host the
# socket's group is 113 — so the agent worker was in a group that owns nothing
# and could not run a container at all. docker-compose.yml says as much beside
# the setting: "the gid differs per host and a wrong one fails at runtime
# rather than at build". This is what makes it fail at DEPLOY time instead.
#
# Read from the socket, not defaulted and not inherited: an exported guess is
# exactly what was wrong. The socket is the only thing that knows.
if ! COMRADE_DOCKER_GID="$(stat -c %g /var/run/docker.sock 2>/dev/null)"; then
  echo "cannot read the group of /var/run/docker.sock, so the workers cannot" \
       "be given a group that reaches the daemon" >&2
  exit 5
fi
export COMRADE_DOCKER_GID
echo "docker socket group: $COMRADE_DOCKER_GID"

# ---------------------------------------------------------------------------
# 4. Build the candidate. Nothing is activated yet.
# ---------------------------------------------------------------------------
$COMPOSE build

# 🔴 (fix.md, hackathon preflight.) The sandbox image is NOT a Compose service,
# so `$COMPOSE build` never touched it and the host kept whatever
# `comrade-sandbox:latest` it happened to have — one with Python and no Node,
# while the candidate's Dockerfile adds both. Every JavaScript repository task
# failed on a release that reported success.
#
# Built here, with the rest of the candidate and before activation, so a broken
# sandbox Dockerfile stops the deploy rather than the first member who asks for
# a repo command.
docker build -f docker/sandbox.Dockerfile -t comrade-sandbox:latest .

# ---------------------------------------------------------------------------
# 4b. Validate the proxy configuration, using the image that will run it.
# ---------------------------------------------------------------------------
#
# 🔴 Nothing checked this, and the ordinary deployment could not parse it. The
# preview site lived in the Caddyfile unconditionally while previews are OFF by
# default, so `COMRADE_PREVIEW_DOMAIN` unset rendered `*.` as a site address
# and `dns` with no arguments:
#
#   Error: adapting config using caddyfile: parsing caddyfile tokens for 'tls':
#   wrong argument count or unexpected line ending after 'dns'
#
# Caddy exits, the public site is down, and step 7 below still passed — it
# talked to the API container directly and never through the proxy. A release
# reported success with the product unreachable.
#
# Validation happens HERE, before activation, so an incomplete or unsupported
# proxy configuration fails while the healthy stack is still serving.
if $COMPOSE config --services | grep -qx caddy; then
  $COMPOSE run --rm --no-deps --entrypoint caddy caddy \
    validate --config /etc/caddy/Caddyfile --adapter caddyfile
fi

# ---------------------------------------------------------------------------
# 5. Migrate, one-off.
# ---------------------------------------------------------------------------
# `run --rm --no-deps`: a one-shot container on the NEW image that does not
# start anything else as a dependency. It runs the `migrate` service, which is
# the ONLY one given COMRADE_DB_URL_ADMIN — the api and both workers have it
# blanked, so the table owner exists in one short-lived container per release
# instead of in three processes that run for weeks.
#
# Migrating through `exec` would need the new stack already running, which is
# the ordering this file exists to prevent. Expand/contract means this is safe against the OLD code still
# serving traffic while it runs.
$COMPOSE run --rm --no-deps -T migrate

# ---------------------------------------------------------------------------
# 6. Activate. The first irreversible step.
# ---------------------------------------------------------------------------
$COMPOSE up -d

# ---------------------------------------------------------------------------
# 7. Readiness, not liveness.
# ---------------------------------------------------------------------------
# /ready checks the schema version and whether a worker is draining the queue,
# so it answers "can this serve a turn" rather than "is the process alive".
for _attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24; do
  if $COMPOSE exec -T api python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:8000/ready', timeout=10)"; then
    ready=yes
    break
  fi
  sleep 5
done

if [ "${ready:-}" != yes ]; then
  echo "Comrade did not become ready; the previous images are still tagged for rollback" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 8. And through the PUBLIC path, which is the one members use.
# ---------------------------------------------------------------------------
#
# 🔴 The check above runs inside the api container against localhost, so it
# says nothing about the proxy in front of it. A Caddy that refused its
# configuration and exited left the site unreachable while this script printed
# "deployed". Skipped when there is no caddy service — the base compose file
# publishes the API directly and there is no second hop to check.
#
# 🔴 (fix.md F35) The replacement for that asked caddy for
# `https://localhost/api/health` with `--no-check-certificate`. Caddy's site is
# `{$COMRADE_HOST}`, matched on Host and SNI, so `localhost` matched no site and
# a HEALTHY deployment failed this gate — while the disabled certificate check
# meant a genuinely broken TLS setup passed it. It also never asked for the
# frontend, so a build that served nothing reached "deployed".
#
# scripts/proxy_check.sh sends the configured hostname with real SNI and real
# certificate verification, and checks both upstreams. See its header for what
# it deliberately does not prove.
if $COMPOSE config --services | grep -qx caddy; then
  # 🔴 (fix.md F35, reopened.) NOT `dirname "$0"`. The workflow pipes this
  # script into `sh -s <sha>`, so `$0` is `sh` and that expression resolved to
  # `./proxy_check.sh` — while the committed helper is at
  # `scripts/proxy_check.sh`. The check I added to stop a healthy deployment
  # being reported broken would itself have failed every healthy deployment.
  #
  # The working tree is the repository this script was just checked out of, and
  # step 2 above operates on it in the current directory, so that is where the
  # helper is.
  if [ ! -f scripts/proxy_check.sh ]; then
    echo "scripts/proxy_check.sh is missing from the checkout at $(pwd)" >&2
    exit 1
  fi

  # 🔴 (fix.md F35, fifth review.) CADDY'S hostname, read from the container
  # that is serving the site — not the first COMRADE_HOST Compose prints.
  #
  # The hostname has to come from the deployment's configuration: Compose
  # interpolates for the containers and exports nothing into the parent SSM
  # shell, so a correctly configured host was reaching the unset-host failure
  # below. Two attempts at that were wrong in the same direction.
  #
  # The first read `.env` with sed — but `.env` is Compose's format, not a
  # shell's. `COMRADE_HOST=comrade.example.test # public hostname` is the
  # hostname alone to Compose and hostname-plus-comment to sed.
  #
  # The second asked `$COMPOSE config` and took the first matching key. Every
  # service with `env_file: [.env]` — api, agent-worker, pipeline-worker —
  # also carries COMRADE_HOST, holding the LITERAL file value. Only caddy is
  # given the interpolated `${COMRADE_HOST}`. Services print alphabetically, so
  # the first match was agent-worker's:
  #
  #   .env: from-dotenv.test    shell: from-shell.test
  #     services.agent-worker.environment.COMRADE_HOST:  from-dotenv.test
  #     services.api.environment.COMRADE_HOST:           from-dotenv.test
  #     services.caddy.environment.COMRADE_HOST:         from-shell.test  <- site
  #     what the release extracted:                      from-dotenv.test
  #
  # So the probe asked for a name Caddy has no site for, which is the original
  # F35 defect reintroduced by its own fix. It also produced the "Compose
  # reverses interpolation precedence" claim in the ledger: I had measured the
  # api's env_file copy, which no shell variable can affect. Ordinary
  # precedence holds. The shell does win for the interpolated value.
  #
  # No parsing this time. docker/Caddyfile's site address is `{$COMRADE_HOST}`,
  # substituted from the container's environment when Caddy loads its config,
  # so the running container's variable IS the name the site is served under.
  # This step already runs after activation, so the container is there to ask.
  #
  # NOT piped into `tr`: a pipeline's status is its LAST command's, so `if !`
  # around a pipe tests the wrong thing — the same mistake that reported this
  # script's own gate as passing.
  if ! caddy_host=$($COMPOSE exec -T caddy printenv COMRADE_HOST); then
    echo "could not read COMRADE_HOST from the running caddy container: it is" \
         "either not running or has no hostname configured" >&2
    exit 1
  fi
  COMRADE_HOST=$(printf '%s' "$caddy_host" | tr -d '\r')
  export COMRADE_HOST
  if [ -z "${COMRADE_HOST:-}" ]; then
    echo "the caddy container has an empty COMRADE_HOST, so the public site" \
         "cannot be checked" >&2
    exit 1
  fi

  if ! sh scripts/proxy_check.sh "$COMRADE_HOST"; then
    echo "the API is ready but the public proxy is not serving it" >&2
    exit 1
  fi
fi

echo "deployed $COMMIT"
exit 0
