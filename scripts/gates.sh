#!/usr/bin/env bash
#
# Every check that must pass before this branch merges.
#
# WHY THIS EXISTS AS A SCRIPT
# -----------------------------
# 🔴 `uv run pytest -q | tail -3 && git commit` does not gate on anything. The
# pipe makes the exit status `tail`'s, which is always 0, so the `&&` runs
# whatever pytest did. That was the shape of a dozen commits on this branch:
# the output was read and was green every time, but the gate itself was
# decorative, and it did once let a commit through on a run showing four
# failures.
#
# `set -euo pipefail` is the whole point of the file. `pipefail` in particular:
# without it any stage that pipes its output has the same bug this replaces.
set -euo pipefail

cd "$(dirname "$0")/.."

RESET=0
QUICK=0
for arg in "$@"; do
  case "$arg" in
    --with-reset) RESET=1 ;;
    --quick)      QUICK=1 ;;
    -h|--help)
      cat <<'USAGE'
usage: scripts/gates.sh [--quick] [--with-reset]

  --quick        skip the slow lanes (browser e2e, real GitHub)
  --with-reset   also rebuild the database from migrations.
                 DESTRUCTIVE: drops every local row, including any repository
                 you have connected. Off by default for that reason.
USAGE
      exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

# ---------------------------------------------------------------------------
# A running worker silently breaks the suite
# ---------------------------------------------------------------------------
# 🔴 Learned the hard way: a live `python -m pipeline.worker` drains the job
# queue that the queue tests are trying to drain themselves, so four unrelated
# tests failed and looked like a regression in code that was fine. Refused
# rather than warned about, because the failure it causes points nowhere near
# its cause.
if command -v powershell.exe >/dev/null 2>&1; then
  if powershell.exe -NoProfile -Command \
       "if (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*pipeline.worker*' }) { exit 1 } else { exit 0 }" \
       >/dev/null 2>&1; then :; else
    echo "a pipeline.worker is running; it will steal jobs the queue tests enqueue." >&2
    echo "stop it and re-run." >&2
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# The stack the tests talk to
# ---------------------------------------------------------------------------
# 🔴 A realtime test failed mid-run with "no realtime event within 5s" and
# passed on the next attempt. The container reported healthy throughout: the
# database had been restarted earlier, which invalidates the logical
# replication slot realtime reads from, and nothing about that is visible from
# `docker ps`.
#
# A flaky gate is a gate people learn to ignore, which is the same failure as
# one that does not gate. Checked as a PRECONDITION so an unready stack says so
# up front, instead of surfacing four minutes in as a test that looks broken.
#
# Only the containers the suite actually needs. edge_runtime and vector are not
# among them, and failing on those would block a legitimate run.
for svc in db realtime rest auth storage; do
  name="supabase_${svc}_Comrade"
  status="$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo missing)"
  if [ "$status" != "running" ]; then
    echo "supabase_${svc} is '${status}', not running. Start the stack with" >&2
    echo "  npx supabase start" >&2
    exit 1
  fi
done

step "backend (pytest)"
uv run pytest -q

step "frontend typecheck"
(cd frontend && npx tsc --noEmit)

step "frontend unit + component"
(cd frontend && npm test -- --run)

step "frontend integration"
(cd frontend && npm run test:integration)

if [ "$QUICK" -eq 0 ]; then
  step "browser journeys (playwright)"
  (cd frontend && npm run test:e2e)

  # The lane that does not fake its dependencies. Everything above runs against
  # a local bare repository through the `_url_for` and `_create_pr` seams —
  # which is exactly where the empty-repo, CRLF and force-with-lease bugs hid.
  # Skips itself cleanly when no credential is configured.
  step "real GitHub end to end"
  uv run pytest -m realgithub -q
fi

if [ "$RESET" -eq 1 ]; then
  step "migrations from empty"
  npx supabase db reset

  # 🔴 The reset drops comrade_authenticator and the passwords on the other
  # three worker roles, because they are created by a SCRIPT rather than by a
  # migration. HANDOFF has said "after a db reset" beside that step for as long
  # as the roles have existed — but this gate did not do it, so the first
  # --with-reset run reported a wall of failures that read as 45 migrations
  # having broken the schema. They had not: the migrations applied cleanly and
  # the suite could simply no longer log in.
  #
  # A documented manual step an automated check forgets is the same shape as a
  # handler registered on import that main() never imports: two places that
  # must agree, failing somewhere other than where it is caused.
  step "worker login roles (the reset drops them)"
  uv run python scripts/restore_local_roles.py

  step "backend against a database built only from migrations"
  uv run pytest -q
fi

printf '\n\033[1;32mall gates passed\033[0m\n'
