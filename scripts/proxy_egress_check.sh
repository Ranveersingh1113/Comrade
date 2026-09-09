#!/bin/sh
# Does the registry proxy allow exactly what the install recipes need, and
# refuse everything else? Run against the topology run_setup builds: an
# --internal network with the proxy as its only route out.
#
# 🔴 A DENIAL MUST BE SQUID'S 403, not a failure to reach squid. The first
# version of this script accepted any exception as "blocked" — and the proxy
# container was dying at startup, so every deny case "passed" on a DNS failure
# while the allow cases failed for the same reason. Reaching the intended
# failure stage is the assertion.
set -u

NET="comrade-proxytest-$$"
PROXY="comrade-proxytest-proxy-$$"
NEIGHBOUR="comrade-proxytest-neighbour-$$"
IMAGE="${1:-comrade-registry-proxy:test}"
CLIENT="${2:-python:3.12-slim}"

cleanup() {
  docker rm -f "$PROXY" "$NEIGHBOUR" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
}
trap cleanup EXIT

# The proxy sits on the DEFAULT bridge (so it has a route out) and on the
# internal network (so the sandbox can reach it). That dual attachment is the
# design: it is the gateway, and the allowlist is what makes that safe.
docker network create --internal "$NET" >/dev/null
docker run -d --name "$PROXY" "$IMAGE" >/dev/null
docker network connect "$NET" "$PROXY" >/dev/null

i=0
while [ "$i" -lt 25 ]; do
  docker logs "$PROXY" 2>&1 | grep -q "Accepting HTTP Socket" && break
  i=$((i + 1)); sleep 1
done
if ! docker logs "$PROXY" 2>&1 | grep -q "Accepting HTTP Socket"; then
  echo "*** the proxy never started; nothing below would mean anything"
  docker logs "$PROXY" 2>&1 | tail -10
  exit 1
fi
if [ "$(docker inspect -f '{{.State.Status}}' "$PROXY")" != "running" ]; then
  echo "*** the proxy is not running; nothing below would mean anything"; exit 1
fi

# A neighbour on the internal network, standing in for another team's sandbox or
# any Comrade container the proxy can itself reach.
docker run -d --name "$NEIGHBOUR" --network "$NET" "$CLIENT" \
  python -c "
import http.server, socketserver
socketserver.TCPServer(('', 8080), http.server.SimpleHTTPRequestHandler).serve_forever()
" >/dev/null

# PRECONDITION: the client can reach the proxy at all. Without this every
# 'denied' below is indistinguishable from a broken harness.
reach=$(docker run --rm --network "$NET" "$CLIENT" python -c "
import socket
s = socket.create_connection(('$PROXY', 3128), timeout=10); s.close(); print('REACHABLE')
" 2>&1 | tail -1)
if [ "$reach" != "REACHABLE" ]; then
  echo "*** the client cannot reach the proxy: $reach"; exit 1
fi
echo "precondition: the client can open a TCP connection to the proxy   OK"
echo

FAILED=0
probe() {
  label="$1"; url="$2"; want="$3"
  out=$(docker run --rm --network "$NET" \
        -e "HTTP_PROXY=http://$PROXY:3128" -e "HTTPS_PROXY=http://$PROXY:3128" \
        -e "http_proxy=http://$PROXY:3128" -e "https_proxy=http://$PROXY:3128" \
        -e "NO_PROXY=localhost,127.0.0.1" \
        "$CLIENT" python -c "
import urllib.error, urllib.request
try:
    r = urllib.request.urlopen('$url', timeout=30)
    print('ALLOWED http', r.status)
except urllib.error.HTTPError as e:
    print(('DENIED' if e.code == 403 else 'ALLOWED'), 'http', e.code)
except Exception as e:
    m = str(e)
    print(('DENIED' if '403' in m else 'UNREACHED'), type(e).__qualname__, m[:60])
" 2>&1 | tail -1)
  case "$out" in
    $want*) printf '  %-46s %-8s %s\n' "$label" "PASS" "$out" ;;
    *)      printf '  %-46s %-8s %s\n' "$label" "***FAIL" "$out"; FAILED=1 ;;
  esac
}

echo "=== ALLOWED: exactly what the install recipes need ==="
probe "pypi.org index (pip, uv, poetry)"        "https://pypi.org/simple/" "ALLOWED"
probe "files.pythonhosted.org (archives)"       "https://files.pythonhosted.org/" "ALLOWED"
probe "registry.npmjs.org (npm, pnpm)"          "https://registry.npmjs.org/leftpad" "ALLOWED"

echo
echo "=== DENIED, and denied BY THE PROXY (403), not by being unreachable ==="
probe "cloud metadata by address"               "http://169.254.169.254/latest/meta-data/" "DENIED"
probe "cloud metadata by name"                  "http://metadata.google.internal/" "DENIED"
probe "an arbitrary destination, https"         "https://example.com/" "DENIED"
probe "an arbitrary destination, http"          "http://example.com/" "DENIED"
probe "a look-alike registry domain"            "http://pypi.org.evil.test/" "DENIED"
probe "a neighbour on the internal network"     "http://$NEIGHBOUR:8080/" "DENIED"
probe "squid's own management interface"        "http://$PROXY:3128/squid-internal-mgr/info" "DENIED"
probe "an allowlisted name on a non-443 port"   "https://pypi.org:8443/" "DENIED"

echo
echo "=== and the network itself has no other route out ==="
direct=$(docker run --rm --network "$NET" "$CLIENT" python -c "
import urllib.request
try:
    urllib.request.urlopen('https://pypi.org/simple/', timeout=15); print('REACHED')
except Exception as e:
    print('NO ROUTE', type(e).__qualname__)
" 2>&1 | tail -1)
case "$direct" in
  "NO ROUTE"*) printf '  %-46s %-8s %s\n' "direct egress with no proxy set" "PASS" "$direct" ;;
  *)           printf '  %-46s %-8s %s\n' "direct egress with no proxy set" "***FAIL" "$direct"; FAILED=1 ;;
esac

echo
echo "=== squid's own log: the policy acting, not a claim that it did ==="
docker logs "$PROXY" 2>&1 | grep -E "TCP_DENIED|TCP_TUNNEL|TCP_MISS|NONE" | tail -14

echo
if [ "$FAILED" -eq 0 ]; then echo "POLICY OK"; else echo "POLICY FAILED"; fi
exit "$FAILED"
