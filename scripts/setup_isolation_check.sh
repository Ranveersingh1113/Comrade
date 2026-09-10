#!/bin/sh
# Can the dependency-install sandbox reach services on the Docker host?
#
# 🔴 THE DEFECT (fix.md F54). `agent/sandbox.py:_internal_network` creates the
# per-run network with `docker network create --internal`. That blocks EXTERNAL
# routing, and it is not the same thing as isolation: an internal bridge still
# has a gateway address on the host, and a container on it can open a TCP
# connection straight to whatever the host has bound there — without asking
# Squid, so the registry allowlist never sees it.
#
# The escape probes in scripts/repo_execution_check.sh could not have caught it.
# They run in the RUN phase, which uses `--network none`, and they aim at
# 172.17.0.1 — the DEFAULT bridge, not the per-run one. Refusals there say
# nothing about the setup phase's own network.
#
# This is the positive control the finding asks for: a listener the host really
# is running, on the address the setup network really does route to, probed from
# a container built the way run_setup builds one.
#
# Usage:  sh scripts/setup_isolation_check.sh [sandbox-image] [proxy-container]
set -u

IMAGE="${1:-comrade-sandbox:latest}"
PROXY="${2:-comrade-registry-proxy}"
NET="comrade-isolation-check-$$"
PORT=9099
FAILED=0
LISTENER=""

say() { printf '  %-54s %-8s %s\n' "$1" "$2" "${3:-}"; }

cleanup() {
  # 🔴 Cleanup failures are reported and FAIL the run (fix.md F55). A check that
  # leaves a network attached to the production proxy, quietly, is worse than one
  # that never ran.
  [ -n "$LISTENER" ] && kill "$LISTENER" 2>/dev/null
  for net in $(docker network ls --format '{{.Name}}' | grep '^comrade-isolation-check-'); do
    docker network disconnect -f "$net" "$PROXY" >/dev/null 2>&1
    if ! docker network rm "$net" >/dev/null 2>&1; then
      echo "  *** could not remove $net"; FAILED=1
    fi
  done
  rm -f /tmp/comrade-f54-*.py
}
trap cleanup EXIT

# 🔴 FILES, not a heredoc on a function definition. That form is re-read on
# every call and does not survive backgrounding, so the first version handed
# python3 no program at all and its own positive control reported
# ConnectionRefusedError against a listener that had never bound. The control
# caught it, which is what a control is for.
cat > /tmp/comrade-f54-listener.py <<'PY'
import socket, sys, time
address, port = sys.argv[1], int(sys.argv[2])
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind((address, port))
server.listen(8)
deadline = time.time() + 180
while time.time() < deadline:
    try:
        server.settimeout(5)
        client, _ = server.accept()
        client.sendall(b"HOST_LISTENER")
        client.close()
    except Exception:
        pass
PY

cat > /tmp/comrade-f54-connect.py <<'PY'
import socket, sys
try:
    s = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=6)
    print(sys.argv[3], s.recv(32))
    s.close()
except Exception as exc:
    print("BLOCKED", type(exc).__name__)
PY

cat > /tmp/comrade-f54-hop.py <<'PY'
import urllib.request
try:
    print("OK", urllib.request.urlopen("https://pypi.org/simple/", timeout=25).status)
except Exception as exc:
    print("FAILED", type(exc).__name__)
PY

probe() {  # network, address -> REACHED / BLOCKED
  docker run --rm --network "$1" --read-only --cap-drop ALL \
    --security-opt no-new-privileges \
    -v /tmp/comrade-f54-connect.py:/probe.py:ro \
    --entrypoint python "$IMAGE" /probe.py "$2" "$PORT" REACHED 2>&1 | tail -1
}

echo "docker: $(docker version --format 'server {{.Server.Version}}')"
echo

# ---------------------------------------------------------------------------
# The topology run_setup builds today.
# ---------------------------------------------------------------------------
CUR="${NET}-current"
docker network create --internal "$CUR" >/dev/null
GW=$(docker network inspect -f '{{(index .IPAM.Config 0).Gateway}}' "$CUR" 2>/dev/null)
echo "=== the network run_setup creates today ==="
echo "    .Internal=$(docker network inspect -f '{{.Internal}}' "$CUR")   gateway=${GW:-<none>}"

if [ -z "$GW" ]; then
  say "a gateway address exists at all" "SKIP" "no gateway; nothing to reach"
else
  python3 /tmp/comrade-f54-listener.py "$GW" "$PORT" >/dev/null 2>&1 &
  LISTENER=$!
  sleep 2
  # POSITIVE CONTROL. Without it, "blocked" below could just mean the listener
  # never started — which is exactly what happened the first time.
  control=$(python3 /tmp/comrade-f54-connect.py "$GW" "$PORT" UP 2>&1 | tail -1)
  case "$control" in
    UP*) say "positive control: the host listener is up" "PASS" "$control" ;;
    *)   say "positive control: the host listener is up" "***FAIL" "$control"
         echo "  nothing below would mean anything"; exit 1 ;;
  esac

  current=$(probe "$CUR" "$GW")
  case "$current" in
    REACHED*) say "today: setup container reaches the host" "SHOWN" "$current" ;;
    *)        say "today: setup container reaches the host" "not seen" "$current" ;;
  esac
fi

# ---------------------------------------------------------------------------
# The topology it should build.
# ---------------------------------------------------------------------------
echo
echo "=== with the gateway removed (isolated gateway mode) ==="
FIX="${NET}-isolated"
if ! docker network create --internal \
       -o com.docker.network.bridge.gateway_mode_ipv4=isolated "$FIX" >/dev/null 2>&1; then
  say "isolated gateway mode is supported" "***FAIL" "daemon refused the option"
  FAILED=1
else
  echo "    .Internal=$(docker network inspect -f '{{.Internal}}' "$FIX")   gateway='$(docker network inspect -f '{{(index .IPAM.Config 0).Gateway}}' "$FIX" 2>/dev/null)'"
  if [ -n "${GW:-}" ]; then
    fixed=$(probe "$FIX" "$GW")
    case "$fixed" in
      BLOCKED*) say "isolated: the same host address is refused" "PASS" "$fixed" ;;
      *)        say "isolated: the same host address is refused" "***FAIL" "$fixed"; FAILED=1 ;;
    esac
  fi

  # And the one thing that must NOT break: the sandbox still has to reach the
  # registry proxy, which is the only route the install is allowed to use.
  if docker network connect "$FIX" "$PROXY" >/dev/null 2>&1; then
    hop=$(docker run --rm --network "$FIX" \
      -e http_proxy="http://$PROXY:3128" -e https_proxy="http://$PROXY:3128" \
      -v /tmp/comrade-f54-hop.py:/hop.py:ro \
      --entrypoint python "$IMAGE" /hop.py 2>&1 | tail -1)
    case "$hop" in
      OK*) say "isolated: the registry proxy still works" "PASS" "$hop" ;;
      *)   say "isolated: the registry proxy still works" "***FAIL" "$hop"; FAILED=1 ;;
    esac
  else
    say "isolated: the proxy can be attached" "***FAIL" "connect refused"; FAILED=1
  fi
fi

echo
if [ "$FAILED" -eq 0 ]; then echo "SETUP ISOLATION OK"; else echo "SETUP ISOLATION FAILED"; fi
exit "$FAILED"
