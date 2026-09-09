#!/bin/sh
# Does repository work actually work, through Comrade's own code, on this host?
#
# Deployment acceptance for fix.md F06/F46 and the hackathon preflight. It drives
# `pipeline.repo_deps.install` and `agent.sandbox.run_contained` — the real
# functions the sync pipeline and the agent call — against a disposable
# repository with both a Python and a Node manifest, and then tries to get out
# of the sandbox on purpose.
#
# WHY IT RUNS THE APP IMAGE RATHER THAN THE FUNCTIONS DIRECTLY. The sandbox is a
# SIBLING container: its bind mounts are resolved by the host daemon, so the
# workspace path inside the caller has to be the same path on the host. Running
# the caller in the app image with /workspaces mounted at /workspaces is the only
# arrangement in which that is true, and it is also what the deployed workers do.
#
# Usage:  sh scripts/repo_execution_check.sh [app-image] [sandbox-image] [proxy-image]
set -u

APP="${1:-comrade-app:candidate}"
SANDBOX="${2:-comrade-sandbox:candidate}"
PROXY_IMAGE="${3:-comrade-registry-proxy:candidate}"

TEAM=11111111-2222-3333-4444-555555555555
REPO_DIR="comrade-test__disposable"
REPO_NAME="comrade-test/disposable"
WS="${COMRADE_WORKSPACES_ROOT:-/workspaces}"
ROOT="$WS/$TEAM/$REPO_DIR"
PROXY="comrade-registry-proxy-verify"
GID=$(stat -c %g /var/run/docker.sock)
FAILED=0

say() { printf '  %-52s %-8s %s\n' "$1" "$2" "${3:-}"; }
check() {  # label, expected-substring, actual
  case "$3" in
    *"$2"*) say "$1" "PASS" "$(printf '%s' "$3" | tr '\n' ' ' | cut -c1-70)" ;;
    *)      say "$1" "***FAIL" "$(printf '%s' "$3" | tr '\n' ' ' | cut -c1-90)"; FAILED=1 ;;
  esac
}

cleanup() {
  docker rm -f "$PROXY" >/dev/null 2>&1
  rm -rf "$WS/$TEAM"
  # The dependency volume this run created, and nothing else: a real team's
  # volume must survive a verification run.
  vol=$(docker run --rm --entrypoint python "$APP" -c "
from shared.workspace import deps_volume
print(deps_volume('$TEAM', '$REPO_NAME'))" 2>/dev/null | tr -d '\r')
  [ -n "$vol" ] && docker volume rm -f "$vol" >/dev/null 2>&1
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# A disposable repository with both manifests.
# ---------------------------------------------------------------------------
rm -rf "$WS/$TEAM"
mkdir -p "$ROOT"
printf 'leftpad==0.1.2\n' > "$ROOT/requirements.txt"
cat > "$ROOT/package.json" <<'JSON'
{ "name": "disposable", "version": "1.0.0", "type": "module",
  "dependencies": { "is-odd": "3.0.1" } }
JSON
cat > "$ROOT/test_disposable.py" <<'PY'
def test_the_installed_python_dependency_imports():
    import leftpad
    assert leftpad.left_pad("7", 3, "0") == "007"
PY
# 🔴 ESM, not require: fix.md F46. Packages live on a volume and execution used
# to rely on NODE_PATH, which Node's ESM resolver ignores entirely — so a
# successful install still left every modern Node app failing with
# ERR_MODULE_NOT_FOUND. This is the assertion that would have caught it.
cat > "$ROOT/esm_check.mjs" <<'JS'
import isOdd from 'is-odd';
if (isOdd(3) !== true) { throw new Error('is-odd disagreed'); }
console.log('ESM_IMPORT_RESOLVED');
JS
cat > "$ROOT/escape_check.py" <<'PY'
# Tries to leave the sandbox four ways. Each must fail.
import socket, urllib.request
for label, url in (
    ("metadata", "http://169.254.169.254/latest/meta-data/"),
    ("arbitrary", "https://pypi.org/simple/"),
):
    try:
        urllib.request.urlopen(url, timeout=8)
        print(f"REACHED_{label}")
    except Exception:
        print(f"BLOCKED_{label}")
for label, host, port in (
    ("platform_db", "127.0.0.1", 54322),
    ("host_gateway", "172.17.0.1", 22),
):
    try:
        socket.create_connection((host, port), timeout=5).close()
        print(f"REACHED_{label}")
    except Exception:
        print(f"BLOCKED_{label}")
PY
( cd "$ROOT" && git init -q . && git add -A \
  && git -c user.email=t@t -c user.name=t commit -qm init )
chown -R 10001:10001 "$WS/$TEAM"

# ---------------------------------------------------------------------------
# The proxy, under a name of its own so Compose still owns the real one.
# ---------------------------------------------------------------------------
docker rm -f "$PROXY" >/dev/null 2>&1
docker run -d --name "$PROXY" "$PROXY_IMAGE" >/dev/null
i=0
while [ "$i" -lt 25 ]; do
  docker logs "$PROXY" 2>&1 | grep -q "Accepting HTTP Socket" && break
  i=$((i + 1)); sleep 1
done
if ! docker logs "$PROXY" 2>&1 | grep -q "Accepting HTTP Socket"; then
  echo "*** the verification proxy never started; nothing below would mean anything"
  docker logs "$PROXY" 2>&1 | tail -8; exit 1
fi

D=postgresql://u:p@127.0.0.1:5432/none
app() {  # run python in the app image, wired as a worker is
  docker run --rm --group-add "$GID" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$WS":"$WS" \
    -e COMRADE_WORKSPACES_ROOT="$WS" \
    -e COMRADE_SANDBOX_IMAGE="$SANDBOX" \
    -e COMRADE_SETUP_PROXY_CONTAINER="$PROXY" \
    -e COMRADE_SETUP_PROXY_URL="http://$PROXY:3128" \
    -e COMRADE_AGENT_DB_URL="$D" -e COMRADE_EXECUTOR_DB_URL="$D" \
    -e COMRADE_PIPELINE_DB_URL="$D" -e COMRADE_CONTROL_DB_URL="$D" \
    -e COMRADE_AUTHENTICATOR_DB_URL="$D" -e COMRADE_DB_URL_ADMIN="$D" \
    -e SUPABASE_URL=http://127.0.0.1 -e SUPABASE_ANON_KEY=x \
    -e SUPABASE_SECRET_KEY=x -e SUPABASE_JWT_SECRET=x \
    --entrypoint python "$APP" -c "$1" 2>&1 | grep -v onnxruntime
}

echo "=== 1. INSTALL, through pipeline.repo_deps.install ==="
out=$(app "
import json, time
from pipeline.repo_deps import install, recipe_for
from shared.workspace import repo_checkout
root = repo_checkout('$TEAM', '$REPO_NAME')
print('RECIPE', getattr(recipe_for(root), 'manifest', None))
t = time.time()
r = install('$TEAM', '$REPO_NAME')
print('ELAPSED %.1f' % (time.time() - t))
print('STATUS', r.get('status'))
print('DETAIL', str(r.get('detail') or r.get('stderr') or '')[:300])
")
printf '%s\n' "$out" | sed 's/^/    /'
check "install reports ok" "STATUS ok" "$(printf '%s' "$out" | grep '^STATUS' || true)"

echo
echo "=== 2. RUN, through agent.sandbox.run_contained (network off) ==="
run_one() {
  app "
import json
from agent.sandbox import run_contained
from pipeline.repo_deps import volume_for
from shared.workspace import repo_checkout
root = repo_checkout('$TEAM', '$REPO_NAME')
vol = volume_for('$TEAM', '$REPO_NAME')
r = run_contained($1, root=root, deps=vol)
print('EXIT', r.get('exit_code'))
print((r.get('stdout') or '') + (r.get('stderr') or ''))
"
}
pytest_out=$(run_one "['pytest','-q','test_disposable.py']")
printf '%s\n' "$pytest_out" | sed 's/^/    /' | tail -6
check "the repository's tests run and pass" "EXIT 0" "$(printf '%s' "$pytest_out" | grep '^EXIT' || true)"

esm_out=$(run_one "['node','esm_check.mjs']")
printf '%s\n' "$esm_out" | sed 's/^/    /' | tail -4
check "Node ESM import resolves (F46)" "ESM_IMPORT_RESOLVED" "$esm_out"

echo
echo "=== 3. ESCAPE ATTEMPTS from inside the run phase ==="
escape=$(run_one "['python','escape_check.py']")
printf '%s\n' "$escape" | sed 's/^/    /' | tail -6
for target in metadata arbitrary platform_db host_gateway; do
  check "run phase cannot reach $target" "BLOCKED_$target" "$escape"
done

echo
echo "=== 4. what the proxy allowed and refused during the install ==="
docker logs "$PROXY" 2>&1 | grep -E "TCP_TUNNEL|TCP_DENIED" | tail -10 | sed 's/^/    /'
allowed=$(docker logs "$PROXY" 2>&1 | grep -c "TCP_TUNNEL" || true)
echo "    tunnels opened to allowlisted registries: $allowed"
[ "$allowed" -gt 0 ] || { say "the install actually used the proxy" "***FAIL" "no tunnels"; FAILED=1; }

echo
if [ "$FAILED" -eq 0 ]; then echo "REPO EXECUTION OK"; else echo "REPO EXECUTION FAILED"; fi
exit "$FAILED"
