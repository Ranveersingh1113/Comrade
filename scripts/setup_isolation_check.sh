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
# The escape probes in scripts/repo_execution_check.sh did not catch this. They
# run in the RUN phase, which uses `--network none`, and they aim at
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
}
trap cleanup EXIT

# The listener. Bound to the per-run bridge's own gateway address, so it stands
# in for anything the host has on that interface without touching a real service.
start_listener() {  # address
  python3 - "$1" "$PORT" >/dev/null 2>&1 &
  LISTENER=$!
} <<'PY'
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

probe() {  # network, address -> prints REACHED or BLOCKED
  docker run --rm --network "$1" --read-only --cap-drop ALL \
    --security-opt no-new-privileges --entrypoint python "$IMAGE" \
    -c "$(printf 'import socket\ntry:\n    s = socket.create_connection((%s, %s), timeout=6)\n    print("REACHED", s.recv(32))\nexcept Exception as exc:\n    print("BLOCKED", type(exc).__name__)\n' "'$2'" "$PORT")" 2>&1 | tail -1
}

make_net() {  # name, extra opts
  shift_opts="$2"
  # shellcheck disable=SC2086
  docker network create --internal $shift_opts "$1" >/dev/null 2>&1
}

echo "docker: $(docker version --format 'server {{.Server.Version}}')"
echo

# ---------------------------------------------------------------------------
# The topology run_setup builds today.
# ---------------------------------------------------------------------------
CUR="${NET}-current"
make_net "$CUR" ""
GW=$(docker network inspect -f '{{(index .IPAM.Config 0).Gateway}}' "$CUR" 2>/dev/null)
echo "=== the network run_setup creates today ==="
echo "    .Internal=$(docker network inspect -f '{{.Internal}}' "$CUR")   gateway=${GW:-<none>}"

if [ -z "$GW" ]; then
  say "a gateway address exists at all" "SKIP" "no gateway; nothing to reach"
else
  start_listener "$GW"
  sleep 2
  # POSITIVE CONTROL: the listener must be reachable from the host itself, or a
  # 'blocked' below would only mean the listener never started.
  control=$(python3 -c "
import socket
try:
    s = socket.create_connection(('$GW', $PORT), timeout=5); print('UP', s.recv(16)); s.close()
except Exception as exc:
    print('DOWN', type(exc).__name__)
" 2>&1 | tail -1)
  case "$control" in
    UP*) say "positive control: the host listener is up" "PASS" "$control" ;;
    *)   say "positive control: the host listener is up" "***FAIL" "$control"
         echo "  nothing below would mean anything"; exit 1 ;;
  esac

  current=$(probe "$CUR" "$GW")
  case "$current" in
    REACHED*) say "today: setup container reaches the host" "SHOWN" "$current  <- the defect" ;;
    *)        say "today: setup container reaches the host" "not seen" "$current" ;;
  esac
fi

# ---------------------------------------------------------------------------
# The topology it should build.
# ---------------------------------------------------------------------------
echo
echo "=== with the gateway removed (isolated gateway mode) ==="
FIX="${NET}-isolated"
if ! make_net "$FIX" "-o com.docker.network.bridge.gateway_mode_ipv4=isolated"; then
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
      --entrypoint python "$IMAGE" -c "
import urllib.request
try:
    print('OK', urllib.request.urlopen('https://pypi.org/simple/', timeout=25).status)
except Exception as exc:
    print('FAILED', type(exc).__name__)
" 2>&1 | tail -1)
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
