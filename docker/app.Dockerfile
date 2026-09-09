# The API and both workers. ONE image, three commands.
#
# They share every dependency and all of shared/, so three Dockerfiles would be
# three copies of the same layers drifting apart — and the failure that causes
# is the one this project keeps producing: two places that must agree, failing
# somewhere other than where it is caused. The command is what differs, and the
# command belongs in the deployment, not the image.
#
# NOT the sandbox image. docker/sandbox.Dockerfile is what a TEAM'S code runs
# in; this is what Comrade runs in. Keeping them separate is the point — this
# one holds credentials and that one must never.
FROM python:3.12-slim

# git is a runtime dependency, not a build one: repo_sync clones and
# ensure_thread_checkout makes worktrees by shelling out to it.
# 🔴 THE DOCKER CLI IS A RUNTIME DEPENDENCY, and its absence was invisible.
# agent/sandbox.py and agent/processes.py shell out to `docker` — that is how a
# team's tests run and how a preview starts — and this image did not contain
# it. Every repo_run, every process_start and every dependency install would
# have failed in production with "Docker is not available", which reads like a
# daemon problem rather than a missing binary.
#
# The CLI only; the DAEMON stays on the host. The workers reach it over the
# mounted socket, and which services get that socket is an explicit choice in
# compose rather than a property of this image.
#
# 🔴 AND postgresql-client, because scripts/backup.py could not run in
# production without it. `docs/deployment.md` requires a rehearsed restore and
# scripts/backup.py implements it, but the image had no pg_dump — so on the
# pilot host the tool fell through to its container fallback, which F37 makes it
# correctly refuse for a REMOTE database. The result was documented backup
# tooling that worked on a developer's machine and nowhere else.
#
# Debian trixie's own postgresql-client is 17.11, and the deployed server is
# 17.6: pg_dump must be at least the server's version, so this needs checking
# again if the server is upgraded. No extra apt source for it.
RUN apt-get update  && apt-get install -y --no-install-recommends       git ca-certificates curl gnupg postgresql-client  && install -m 0755 -d /etc/apt/keyrings  && curl -fsSL https://download.docker.com/linux/debian/gpg       -o /etc/apt/keyrings/docker.asc  && chmod a+r /etc/apt/keyrings/docker.asc  && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable"       > /etc/apt/sources.list.d/docker.list  && apt-get update  && apt-get install -y --no-install-recommends docker-ce-cli  && apt-get purge -y curl gnupg && apt-get autoremove -y  && rm -rf /var/lib/apt/lists/*

# uv, pinned. The lockfile is the reproducibility claim and a floating
# installer is a way to lose it quietly.
COPY --from=ghcr.io/astral-sh/uv:0.5.14 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, as their own layer: application code changes on every
# deploy and the dependency set does not, so this is the difference between a
# ten-second build and a four-minute one.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY agent/ agent/
COPY evaluation/ evaluation/
COPY pipeline/ pipeline/
COPY server/ server/
COPY shared/ shared/
COPY supabase/migrations/ supabase/migrations/
RUN uv sync --frozen --no-dev

# The same uid the sandbox runs as (agent/sandbox.SANDBOX_UID). Files a
# container writes into a thread's worktree are then owned by the user that
# owns the worktree, which is what stops the worker being unable to clean up
# after the code it ran.
RUN useradd --create-home --uid 10001 comrade \
 && mkdir -p /workspaces && chown comrade:comrade /workspaces
USER comrade

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    COMRADE_WORKSPACES_ROOT=/workspaces

# Overridden per service in compose. The API is the default because it is the
# one an operator runs by hand to check the image.
EXPOSE 8000
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000"]
