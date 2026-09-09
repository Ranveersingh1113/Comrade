#!/bin/sh
# Does the PUBLIC site actually serve this deployment?
#
# 🔴 THE DEFECT (fix.md F35). The release used to ask the caddy container for
# `https://localhost/api/health` with `--no-check-certificate`, and both halves
# of that are wrong:
#
#   * Caddy's site is `{$COMRADE_HOST}` — a NAMED site, matched on the Host
#     header and SNI. A request for `localhost` matches no site, so Caddy
#     answers 404 and a perfectly healthy deployment was reported as "the API
#     is ready but the public proxy is not serving it".
#   * `--no-check-certificate` made every TLS failure invisible: an expired
#     certificate, a name that does not match, a broken ACME renewal. The one
#     thing a proxy check is for.
#
# It also only ever asked for /api/health, so a frontend that failed to build
# or was not being served passed the release gate.
#
# WHAT THIS IS AND IS NOT. `--resolve` sends the CONFIGURED hostname — real Host
# header, real SNI, real certificate verification — while connecting to a local
# address. That proves the proxy, the certificate and both upstreams. It does
# NOT prove public DNS or that anything outside this host can reach the site;
# that is a different check and the output says so rather than implying it.
#
# Usage: proxy_check.sh <host> [connect-address] [attempts]
set -eu

host="${1:?usage: proxy_check.sh <host> [connect-address] [attempts]}"
addr="${2:-127.0.0.1}"
attempts="${3:-12}"
port="${COMRADE_PROXY_PORT:-443}"

# A deployment using Caddy's internal CA rather than a publicly trusted one can
# point at its root here. Absent, the system trust store decides — which is the
# correct default, and the reason the old check proved nothing.
ca_args=""
if [ -n "${COMRADE_TLS_CA:-}" ]; then
  ca_args="--cacert ${COMRADE_TLS_CA}"
fi

# `localhost` cannot be the configured host: Caddy would have no site for it,
# which is exactly the bug this replaces. Fail loudly rather than checking
# something meaningless.
case "$host" in
  localhost|127.0.0.1|"")
    echo "COMRADE_HOST is '$host'; the proxy serves a named site and cannot be" \
         "checked through localhost" >&2
    exit 2 ;;
esac

# `--resolve` is keyed on the PORT as well as the name, so a non-default port
# has to appear in the URL too or curl looks the hostname up for real — which is
# how this first failed its own test, with "could not resolve host".
authority="$host"
[ "$port" = 443 ] || authority="${host}:${port}"

probe() {
  # shellcheck disable=SC2086 - ca_args is deliberately word-split or empty
  curl --silent --show-error --fail --location \
    --max-time 15 \
    --resolve "${host}:${port}:${addr}" \
    ${ca_args} \
    -o /dev/null \
    "https://${authority}${1}"
}

describe() {
  case "$1" in
    6)  echo "the hostname could not be resolved" ;;
    7)  echo "nothing is listening on ${addr}:${port}" ;;
    22) echo "the proxy answered with an error status" ;;
    28) echo "the proxy did not answer in time" ;;
    35|60) echo "the certificate for ${host} was not accepted" ;;
    *)  echo "curl exited ${1}" ;;
  esac
}

attempt=1
while [ "$attempt" -le "$attempts" ]; do
  api_status=0
  probe "/api/health" || api_status=$?
  if [ "$api_status" -eq 0 ]; then
    # The API is up through the proxy. The frontend is a SEPARATE upstream and
    # a separate failure: a build that produced nothing still serves 404 here
    # while /api/health is perfectly healthy.
    app_status=0
    probe "/" || app_status=$?
    if [ "$app_status" -eq 0 ]; then
      echo "proxy serving ${host} (checked from this host via ${addr}:${port};" \
           "public DNS and routing not verified)"
      exit 0
    fi
    last="the site is served but the frontend is not: $(describe "$app_status")"
  else
    last="$(describe "$api_status")"
  fi
  attempt=$((attempt + 1))
  [ "$attempt" -le "$attempts" ] && sleep 5
done

echo "the public proxy is not serving ${host}: ${last}" >&2
exit 1
