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
AGENT_EVAL=0
for arg in "$@"; do
  case "$arg" in
    --with-reset)      RESET=1 ;;
    --quick)           QUICK=1 ;;
    --with-agent-eval) AGENT_EVAL=1 ;;
    -h|--help)
      cat <<'USAGE'
usage: scripts/gates.sh [--quick] [--with-reset] [--with-agent-eval]

  --quick             skip the slow lanes (browser e2e, real GitHub)
  --with-reset        also rebuild the database from migrations.
                       DESTRUCTIVE: drops every local row, including any
                       repository you have connected. Off by default for
                       that reason.
  --with-agent-eval   run the team-scenario scorer tests plus the live
                       four-person scenario (sim/scenario.py --check)
                       against the real API and a real GitHub repo. Minutes
                       long; needs sim/setup.py to have been run at least
                       once already. Off by default for that reason.
USAGE
      exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

# ---------------------------------------------------------------------------
# The processes the agent-eval lane owns
# ---------------------------------------------------------------------------
# 🔴 (fix.md F63, reopened) `kill` on a `uv run` reaches the WRAPPER. The
# python it spawned keeps :8000, and the next invocation of this script meets
# it at the backend precondition — a refusal pointing nowhere near the lane
# that leaked it. So the interpreter is resolved once and each server below is
# started directly: the pid we hold is the pid that serves.
lane_pids=""

#: How long an owned process may take to finish the job in hand after TERM.
#: 🔴 Both workers DRAIN on their first TERM — they finish the item they are
#: holding and then exit — so this is a real wait, not a formality, and model
#: or compiler work can outlast it. Overridable only so a test can exercise the
#: escalation below without waiting half a minute for it.
LANE_DRAIN_SECONDS="${LANE_DRAIN_SECONDS:-30}"

lane_survivors() {
  survivors=""
  for pid in $lane_pids; do
    kill -0 "$pid" 2>/dev/null && survivors="$survivors $pid"
  done
  printf '%s' "$survivors"
}

lane_stop() {
  [ -n "$lane_pids" ] || return 0
  kill $lane_pids 2>/dev/null || true
  # Waited for, not fired and forgotten: a delivered signal is not an exited
  # process, and these two deliberately finish their current job first.
  waited=0
  while [ "$waited" -lt "$LANE_DRAIN_SECONDS" ] && [ -n "$(lane_survivors)" ]; do
    sleep 1
    waited=$((waited + 1))
  done
  # 🔴 ESCALATED, NOT FORGOTTEN (fix.md F63, third pass). This used to clear
  # `lane_pids`, echo the survivors and return ZERO — so a pipeline or agent
  # worker that outlived TERM kept writing to the database while the gate
  # printed `all gates passed`, and clearing the list stopped the EXIT trap
  # from ever trying again. Only pids this lane started are ever signalled;
  # nothing here matches on process names.
  stubborn="$(lane_survivors)"
  if [ -n "$stubborn" ]; then
    echo "did not stop within ${LANE_DRAIN_SECONDS}s, killing:$stubborn" >&2
    kill -9 $stubborn 2>/dev/null || true
  fi
  # Reaped, or a zombie reads as a live process to `kill -0` below.
  for pid in $lane_pids; do wait "$pid" 2>/dev/null || true; done
  stubborn="$(lane_survivors)"
  if [ -n "$stubborn" ]; then
    echo "OWNED PROCESSES STILL RUNNING:$stubborn" >&2
    return 1
  fi
  lane_pids=""
  return 0
}

#: The gate's exit status and the cleanup's, merged. A cleanup that could not
#: establish termination fails the run; a scenario that already failed keeps
#: its own status.
lane_exit() {
  status=$?
  lane_stop || status=1
  trap - EXIT INT TERM
  exit "$status"
}

#: An interrupted gate has not passed. The trap used to run cleanup and then
#: let the shell decide, which on some shells is a zero exit.
lane_interrupted() {
  lane_stop || true
  trap - EXIT INT TERM
  exit "$1"
}

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

# 🔴 THE DATABASE ALONE (fix.md F57). The backend suite seeds and cleans a
# fixed set of teams per test, so ANY other process on the same database is a
# competing writer. A long-running API is the one an operator is most likely to
# have up — and this file's own browser lane used to tell them to start it,
# which is how the trap gets set.
#
# Measured, same commit, two runs an hour apart:
#   API down   1722 passed
#   API up     1721 passed, 1 failed — test_agent_history, the seeded thread
#              came back missing a message it had just written
# and that test passes 3/3 on its own with the API up, so it is contention
# rather than the API's mere existence.
#
# Refused rather than warned: a flake that appears in one lane and points at
# another costs far more than being told to stop a server.
if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
  echo "an API is already answering on :8000." >&2
  echo "the backend suite needs the database to itself — a second writer makes" >&2
  echo "it fail intermittently somewhere unrelated. Stop it and rerun; the" >&2
  echo "browser lane below starts its own and stops it again." >&2
  exit 1
fi

step "backend (pytest)"
uv run pytest -q

# 🔴 `npm run build`, NOT `npx tsc --noEmit`. They resolve different TypeScript
# configurations — the build runs `tsc -b`, which follows the solution config,
# and --noEmit does not. The typecheck lane was green for weeks while the
# production image could not be built at all: `.catch` on a Supabase thenable
# only fails under `tsc -b`, and it was found by building the Docker image
# rather than by any gate. A lane that does not run the command the deploy runs
# is not checking the deploy.
step "frontend build (the command the deploy runs)"
(cd frontend && npm run build)

step "frontend lint"
(cd frontend && npm run lint)

step "frontend unit + component"
(cd frontend && npm test -- --run)

step "frontend integration"
(cd frontend && npm run test:integration)

if [ "$QUICK" -eq 0 ]; then
  # 🔴 NOT STARTED HERE, and no longer demanded of the operator either
  # (fix.md F57). frontend/playwright.config.ts already starts the API, the
  # agent worker and the dev server as its own `webServer` entries, each with
  # a url it waits on — so this lane was self-sufficient the whole time, and
  # the precondition check that used to sit here only ever told the operator
  # to put a second writer on the database for the lane above.
  #
  # `reuseExistingServer: true` means it would adopt a server that is already
  # running, which is what the old check was really worried about: a pool
  # opened before a `--with-reset` holds handles to a dropped database. The
  # backend guard above refuses to run at all while anything answers on :8000,
  # so by the time we reach here there is nothing stale to adopt and Playwright
  # starts a fresh one.
  step "browser journeys (playwright)"
  (cd frontend && npm run test:e2e)

  # The lane that does not fake its dependencies. Everything above runs against
  # a local bare repository through the `_url_for` and `_create_pr` seams —
  # which is exactly where the empty-repo, CRLF and force-with-lease bugs hid.
  # Skips itself cleanly when no credential is configured.
  step "real GitHub end to end"
  uv run pytest -m realgithub -q
fi

if [ "$AGENT_EVAL" -eq 1 ]; then
  step "agent eval (deterministic scorer tests)"
  uv run pytest tests/test_team_scenario_scoring.py -q

  # 🔴 (fix.md F52) Does it ANSWER? The browser journey accepts an AI message
  # OR an error note, deliberately — the model returns nothing often enough
  # that requiring a reply made it flaky, and "the member is told something
  # either way" is the property that journey guards. It means the default gate
  # can be green with the product silent, which is what the review found: a
  # passing journey in which the ordinary question "What tasks are open right
  # now?" was answered three times with nothing.
  #
  # These make real model calls and assert on facts only the team's own state
  # can supply, so they belong in this lane rather than the default one.
  step "agent eval (does it answer? — live judge prompts)"
  uv run pytest tests/test_agent_usefulness_live.py -q -m live

  # 🔴 THIS LANE OWNS THE WHOLE STACK THE SCENARIO NEEDS (fix.md F63). It used
  # to REFUSE to run unless an API was already answering on :8000 — an API that
  # no invocation of this script can leave there:
  #
  #   * the backend precondition at the top exits 1 while anything answers on
  #     :8000, so it cannot have been started before the run;
  #   * Playwright owns the one the browser lane uses and stops it with the
  #     lane (fix.md F57), so it is gone by the time we reach here;
  #   * `--quick --with-agent-eval` skips the browser lane entirely and never
  #     had one at all.
  #
  # ALL THREE, and the agent worker is the one the first repair forgot.
  # `POST /agent/turn` only ENQUEUES — "agent.worker executes it", says the
  # endpoint — and the pipeline worker does not read that queue. A lane with an
  # API and a pipeline worker admits every turn and executes none of them.
  #
  # The pipeline worker is here because the scenario connects a repository and
  # waits for the clone.
  step "agent eval (live four-person scenario)"
  PY="$(uv run python -c 'import sys; print(sys.executable)')"
  "$PY" -m uvicorn server.app:app --port 8000 >/dev/null 2>&1 &
  api_pid=$!
  "$PY" -m pipeline.worker &
  pipeline_pid=$!
  "$PY" -m agent.worker &
  agent_pid=$!
  lane_pids="$api_pid $pipeline_pid $agent_pid"
  # Every exit path, interruption included. A worker left behind drains the job
  # queue that other gates' tests enqueue, and an API left behind is exactly
  # the second writer the backend precondition refuses to run beside.
  trap lane_exit EXIT
  trap 'lane_interrupted 130' INT
  trap 'lane_interrupted 143' TERM

  # 🔴 `/ready`'s OWN worker check, not a sleep: it reports ok only when BOTH
  # an agent and a pipeline worker have beaten recently, which is precisely the
  # precondition this lane could not previously state. It also cannot answer at
  # all unless the API is up and reaching the database, so one poll covers all
  # three processes.
  for _ in $(seq 1 90); do
    case "$(curl -sS http://localhost:8000/ready 2>/dev/null || echo '')" in
      *'"workers":"ok"'*) break ;;
    esac
    sleep 1
  done
  case "$(curl -sS http://localhost:8000/ready 2>/dev/null || echo '')" in
    *'"workers":"ok"'*) ;;
    *)
      echo "the API, agent worker and pipeline worker this lane started did" >&2
      echo "not all report ready within 90s. /ready says:" >&2
      curl -sS http://localhost:8000/ready 2>/dev/null >&2 || true
      exit 1 ;;
  esac

  uv run python -m sim.scenario --check

  # 🔴 The cleanup's own verdict decides too. The port check below only ever
  # sees the API; a worker that outlives its signal holds no port and would
  # otherwise pass unnoticed straight into `all gates passed`.
  if ! lane_stop; then
    echo "the lane could not stop processes it started; see above." >&2
    trap - EXIT INT TERM
    exit 1
  fi
  trap - EXIT INT TERM
  # And the port, for anything holding it that this lane did not start — a
  # child of one of ours, say. Still a failure rather than a warning: the next
  # invocation refuses to start beside it, at a check pointing nowhere near
  # this lane.
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    echo "something this lane started is still answering on :8000 after" >&2
    echo "cleanup. The next gate run will refuse to start beside it." >&2
    exit 1
  fi
fi

if [ "$RESET" -eq 1 ]; then
  step "migrations from empty"
  npx supabase db reset

  # 🔴 The reset drops comrade_authenticator and the passwords on the other
  # three worker roles, because they are created by a SCRIPT rather than by a
  # migration. The local setup instructions say "after a db reset" beside that
  # step — but this gate did not do it, so the first
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
