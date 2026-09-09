#!/bin/sh
# Does repository work actually work, through Comrade's own code, on this host?
#
# Deployment acceptance for fix.md F06/F46 and the hackathon preflight. It drives
# `pipeline.repo_deps.install` and `agent.sandbox.run_contained` — the real
# functions the sync pipeline and the agent call — against disposable
# repositories, and then tries to get out of the sandbox on purpose.
#
# WHY IT RUNS THE APP IMAGE RATHER THAN THE FUNCTIONS DIRECTLY. The sandbox is a
# SIBLING container: its bind mounts are resolved by the host daemon, so the
# workspace path inside the caller has to be the same path on the host. Running
# the caller in the app image with /workspaces mounted at /workspaces is the only
# arrangement in which that is true, and it is what the deployed workers do.
#
# Usage:  sh scripts/repo_execution_check.sh [app-image] [sandbox-image] [proxy-image]
set -u

APP="${1:-comrade-app:candidate}"
SANDBOX="${2:-comrade-sandbox:candidate}"
PROXY_IMAGE="${3:-comrade-registry-proxy:candidate}"

TEAM=11111111-2222-3333-4444-555555555555
WS="${COMRADE_WORKSPACES_ROOT:-/workspaces}"
PROXY="comrade-registry-proxy-verify"
GID=$(stat -c %g /var/run/docker.sock)
FAILED=0

# 🔴 TWO repositories, not one. `recipe_for` returns the FIRST recipe a checkout
# matches, so a tree carrying both requirements.txt and package.json installs
# Python and never touches npm — the first version of this script "tested" a Node
# ESM import against a volume nothing had put anything Node in, and reported the
# resulting ERR_MODULE_NOT_FOUND as a product failure.
PY_DIR="comrade-test__disposable-py"
PY_NAME="comrade-test/disposable-py"
NODE_DIR="comrade-test__disposable-node"
NODE_NAME="comrade-test/disposable-node"

say() { printf '  %-52s %-8s %s\n' "$1" "$2" "${3:-}"; }
check() {  # label, expected-substring, actual
  case "$3" in
    *"$2"*) say "$1" "PASS" "$(printf '%s' "$3" | tr '\n' ' ' | cut -c1-64)" ;;
    *)      say "$1" "***FAIL" "$(printf '%s' "$3" | tr '\n' ' ' | cut -c1-90)"; FAILED=1 ;;
  esac
}

# The dummy database URLs Settings insists on at import, declared once. Up here
# because `cleanup` needs them too: `shared.workspace` imports config, so without
# them the volume-name helper died on a pydantic ValidationError, printed nothing,
# and the cleanup removed nothing while saying it could not work out the name.
#
# Dummy on purpose. Nothing below touches a database, which is the design point:
# nothing of Comrade's is inside the sandbox, so nothing of Comrade's is needed
# to drive it.
D=postgresql://u:p@127.0.0.1:5432/none
SETTINGS_ENV="-e COMRADE_AGENT_DB_URL=$D -e COMRADE_EXECUTOR_DB_URL=$D
  -e COMRADE_PIPELINE_DB_URL=$D -e COMRADE_CONTROL_DB_URL=$D
  -e COMRADE_AUTHENTICATOR_DB_URL=$D -e COMRADE_DB_URL_ADMIN=$D
  -e SUPABASE_URL=http://127.0.0.1 -e SUPABASE_ANON_KEY=x
  -e SUPABASE_SECRET_KEY=x -e SUPABASE_JWT_SECRET=x"

deps_name() {  # repo full name -> the volume Comrade would use
  # 🔴 Comrade's OWN function, not a second implementation of the hash. The
  # naming rule lives in shared.workspace.deps_volume; a copy here is a copy
  # that can disagree, and this script would then delete nothing — or something
  # else.
  docker run --rm $SETTINGS_ENV --entrypoint python "$APP" -c "
from shared.workspace import deps_volume
print(deps_volume('$TEAM', '$1'))" 2>/dev/null | tail -1 | tr -d '\r'
}

cleanup() {
  docker rm -f "$PROXY" >/dev/null 2>&1
  rm -rf "$WS/$TEAM"
  # Only what this run created. A real team's dependency volume must survive a
  # verification run, so the names are computed rather than pattern-matched.
  #
  # 🔴 `tail -1`, and the removal is not silenced. The app image prints a GPU
  # discovery warning on import; taking the whole of stdout as the volume name
  # gave `docker volume rm` a multi-line argument, which failed — into
  # /dev/null. The volumes survived, the next install correctly reported
  # "current", and two checks failed on a run where the product was right.
  # A cleanup that fails quietly looks exactly like one that worked.
  for repo in "$PY_NAME" "$NODE_NAME"; do
    vol=$(deps_name "$repo")
    case "$vol" in
      comrade-deps-*) docker volume rm -f "$vol" >/dev/null || \
                        echo "    (could not remove $vol)" ;;
      *) echo "    (could not work out the volume name for $repo: '$vol')" ;;
    esac
  done
}
trap cleanup EXIT

# 🔴 REMOVED UP FRONT, not only on the way out. A previous run left its volumes
# behind and the Node install correctly reported "current" — the environment was
# already up to date — so the run made no network request and the proxy log had
# nothing in it. A check whose result depends on whether it has been run before
# is not a check. Starting from no volume makes "installed" and "went through the
# proxy" both meaningful.
cleanup

rm -rf "$WS/$TEAM"

# --- the Python repository -------------------------------------------------
mkdir -p "$WS/$TEAM/$PY_DIR"
# 🔴 pytest IS A DEPENDENCY, and finding that out is the point of running this.
# `python -m venv` makes an ISOLATED environment, so with only six installed the
# venv has no pytest and PATH falls through to the sandbox image's copy — which
# runs under the image interpreter and cannot see the project's site-packages.
# The first version declared only six and read the resulting "No module named
# 'six'" as a product failure. A project that runs its tests declares its runner;
# the 3a check below proves the venv itself is sound either way.
printf 'six==1.17.0\npytest==8.3.4\n' > "$WS/$TEAM/$PY_DIR/requirements.txt"
cat > "$WS/$TEAM/$PY_DIR/test_disposable.py" <<'PY'
def test_the_installed_dependency_imports():
    import six
    assert six.PY3 is True
PY
cat > "$WS/$TEAM/$PY_DIR/escape_check.py" <<'PY'
# Tries to leave the run sandbox four ways. Every one must fail.
import socket, urllib.request
for label, url in (("metadata", "http://169.254.169.254/latest/meta-data/"),
                   ("arbitrary", "https://pypi.org/simple/")):
    try:
        urllib.request.urlopen(url, timeout=8)
        print("REACHED_" + label)
    except Exception:
        print("BLOCKED_" + label)
for label, host, port in (("platform_db", "127.0.0.1", 54322),
                          ("host_gateway", "172.17.0.1", 22)):
    try:
        socket.create_connection((host, port), timeout=5).close()
        print("REACHED_" + label)
    except Exception:
        print("BLOCKED_" + label)
PY

# --- the Node repository ---------------------------------------------------
mkdir -p "$WS/$TEAM/$NODE_DIR"
cat > "$WS/$TEAM/$NODE_DIR/package.json" <<'JSON'
{ "name": "disposable", "version": "1.0.0", "type": "module",
  "dependencies": { "is-odd": "3.0.1" } }
JSON
# 🔴 ESM, not require: fix.md F46. Packages live on a volume and execution used
# to rely on NODE_PATH, which Node's ESM resolver ignores entirely — so a
# successful install still left every modern Node app failing with
# ERR_MODULE_NOT_FOUND. This is the assertion that catches that.
cat > "$WS/$TEAM/$NODE_DIR/esm_check.mjs" <<'JS'
import isOdd from 'is-odd';
if (isOdd(3) !== true) { throw new Error('is-odd disagreed'); }
console.log('ESM_IMPORT_RESOLVED');
JS

for d in "$PY_DIR" "$NODE_DIR"; do
  ( cd "$WS/$TEAM/$d" && git init -q . && git add -A \
    && git -c user.email=t@t -c user.name=t commit -qm init )
done
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

app() {  # run python in the app image, wired the way a worker is
  docker run --rm --group-add "$GID" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$WS":"$WS" \
    -e COMRADE_WORKSPACES_ROOT="$WS" \
    -e COMRADE_SANDBOX_IMAGE="$SANDBOX" \
    -e COMRADE_SETUP_PROXY_CONTAINER="$PROXY" \
    -e COMRADE_SETUP_PROXY_URL="http://$PROXY:3128" \
    $SETTINGS_ENV \
    --entrypoint python "$APP" -c "$1" 2>&1 | grep -v onnxruntime
}

install_one() {  # repo full name
  app "
import time
from pipeline.repo_deps import install, recipe_for, volume_for
from shared.workspace import repo_checkout
root = repo_checkout('$TEAM', '$1')
print('CHECKOUT', root, root.exists())
r = recipe_for(root)
print('RECIPE', getattr(r, 'name', None), getattr(r, 'runtime', None))
t = time.time()
rep = install('$TEAM', '$1')
print('ELAPSED %.1f' % (time.time() - t))
print('STATUS', rep.get('status'))
print('DETAIL', str(rep.get('detail') or '')[:400])
print('VOLUME', volume_for('$TEAM', '$1'))
"
}

run_one() {  # repo full name, python-list argv
  app "
from agent.sandbox import run_contained
from pipeline.repo_deps import volume_for
from shared.workspace import repo_checkout
root = repo_checkout('$TEAM', '$1')
vol = volume_for('$TEAM', '$1')
print('MOUNTED_VOLUME', vol)
r = run_contained($2, root=root, deps=vol)
print('EXIT', r.get('exit_code'))
print((r.get('stdout') or '') + (r.get('stderr') or ''))
"
}

echo "=== 1. INSTALL the Python repository, through pipeline.repo_deps.install ==="
py_install=$(install_one "$PY_NAME")
printf '%s\n' "$py_install" | sed 's/^/    /'
check "python recipe chosen" "RECIPE requirements.txt" "$py_install"
check "python install succeeded" "STATUS installed" "$py_install"
check "a dependency volume was provisioned" "VOLUME comrade-deps-" "$py_install"

echo
echo "=== 2. INSTALL the Node repository ==="
node_install=$(install_one "$NODE_NAME")
printf '%s\n' "$node_install" | sed 's/^/    /'
check "node recipe chosen" "RECIPE package.json" "$node_install"
check "node install succeeded" "STATUS installed" "$node_install"

echo
echo "=== 3a. the installed environment is on the interpreter's path ==="
import_out=$(run_one "$PY_NAME" "['python','-c','import six, sys; print(\"SIX_OK\", sys.prefix)']")
printf '%s\n' "$import_out" | sed 's/^/    /' | tail -4
check "the venv python imports the installed dependency" "SIX_OK" "$import_out"

echo
echo "=== 3b. RUN the repository's own tests (network off) ==="
pytest_out=$(run_one "$PY_NAME" "['pytest','-q','test_disposable.py']")
printf '%s\n' "$pytest_out" | sed 's/^/    /' | tail -8
check "the repository's tests run and pass" "EXIT 0" "$pytest_out"

echo
echo "=== 4. RUN a Node ESM import against the installed dependency (F46) ==="
esm_out=$(run_one "$NODE_NAME" "['node','esm_check.mjs']")
printf '%s\n' "$esm_out" | sed 's/^/    /' | tail -8
check "Node ESM import resolves" "ESM_IMPORT_RESOLVED" "$esm_out"

echo
echo "=== 5. ESCAPE ATTEMPTS from inside the run phase ==="
escape=$(run_one "$PY_NAME" "['python','escape_check.py']")
printf '%s\n' "$escape" | sed 's/^/    /' | tail -7
for target in metadata arbitrary platform_db host_gateway; do
  check "run phase cannot reach $target" "BLOCKED_$target" "$escape"
done

echo
echo "=== 6. what the proxy allowed and refused ==="
docker logs "$PROXY" 2>&1 | grep -E "TCP_TUNNEL|TCP_DENIED" | tail -10 | sed 's/^/    /'
pypi_used=$(docker logs "$PROXY" 2>&1 | grep -c "pypi.org:443" || true)
npm_used=$(docker logs "$PROXY" 2>&1 | grep -c "registry.npmjs.org" || true)
check "the python install went through the proxy" "yes" \
      "$([ "$pypi_used" -gt 0 ] && echo yes || echo "no: $pypi_used tunnels")"
check "the node install went through the proxy" "yes" \
      "$([ "$npm_used" -gt 0 ] && echo yes || echo "no: $npm_used tunnels")"

echo
if [ "$FAILED" -eq 0 ]; then echo "REPO EXECUTION OK"; else echo "REPO EXECUTION FAILED"; fi
exit "$FAILED"
