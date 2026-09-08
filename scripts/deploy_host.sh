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
    echo "deployed $COMMIT"
    exit 0
  fi
  sleep 5
done

echo "Comrade did not become ready; the previous images are still tagged for rollback" >&2
exit 1
