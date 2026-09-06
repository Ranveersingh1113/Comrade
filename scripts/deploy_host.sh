#!/usr/bin/env sh
# Deploy one reviewed Git commit on the single pilot host.
set -eu

[ "$#" -eq 1 ] || { echo "usage: $0 <commit>" >&2; exit 2; }

git fetch --depth=1 origin "$1"
git checkout --detach --force "$1"
mkdir -p /workspaces
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

for attempt in $(seq 1 24); do
  if docker exec comrade-api-1 python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:8000/ready', timeout=10)"; then
    exit 0
  fi
  sleep 5
done

echo "Comrade did not become ready" >&2
exit 1
