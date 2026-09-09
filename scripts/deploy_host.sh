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
# 4. Build the candidate. Nothing is activated yet.
# ---------------------------------------------------------------------------
$COMPOSE build

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
  if [ -z "${COMRADE_HOST:-}" ]; then
    echo "COMRADE_HOST is unset, so the public site cannot be checked" >&2
    exit 1
  fi
  if ! sh "$(dirname "$0")/proxy_check.sh" "$COMRADE_HOST"; then
    echo "the API is ready but the public proxy is not serving it" >&2
    exit 1
  fi
fi

echo "deployed $COMMIT"
exit 0
