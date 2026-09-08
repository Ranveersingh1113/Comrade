# The image the team's code runs in (agent/sandbox.py).
#
# WHY THERE IS AN IMAGE AT ALL, RATHER THAN python:3.12-slim
# ------------------------------------------------------------
# repo_run's allowlist named pytest, ruff and mypy while the stock image had
# none of them, so two thirds of what the tool advertised failed on contact.
# Advertising a capability you do not have is worse than not having it: the
# agent spends a turn discovering it, every time.
#
# WHAT IS DELIBERATELY NOT HERE
# -------------------------------
# The team's OWN dependencies. A repository with a requirements.txt or a
# package.json needs those installed, and there is no network inside the
# container, on purpose -- exfiltration is the failure that leaves no trace in
# a diff. The setup phase (agent/sandbox.py:run_setup) installs them WITH the
# network, through a registry proxy, into a separate volume; the run phase
# mounts that volume read-only with the network off.
FROM python:3.12-slim

# 🔴 NODE, BECAUSE THE PRODUCT SAYS IT INSTALLS NODE (fix.md F06).
#
# pipeline/repo_deps.py advertises npm, pnpm and package.json recipes. This
# image was python:3.12-slim, so every one of them failed with
# "npm: not found" -- from the sync pipeline, where nobody sees it, leaving a
# JavaScript repository with an empty dependency volume and an agent reporting
# module-not-found as if it were the team's bug.
#
# Copied from the official node image rather than installed from Debian, whose
# bookworm nodejs is two majors behind and would fail lockfileVersion 3.
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 # pnpm at BUILD time, not through corepack at install time: corepack fetches
 # the manager itself on first use, and the setup phase's egress policy is a
 # registry proxy, not the internet. A recipe that needs an unlisted host to
 # bootstrap is a recipe that fails closed in production and works here.
 && npm install --global --no-fund --no-audit pnpm@9.15.4 \
 && npm cache clean --force

# --no-cache-dir because the layer is never reused for anything: this image is
# built once and read many times.
RUN pip install --no-cache-dir \
    pytest==8.3.4 \
    ruff==0.9.2 \
    mypy==1.14.1

# Nothing runs as root that does not have to. The container also has no
# network, no capabilities and a read-only rootfs (see agent/sandbox.py), but
# a non-root uid is what keeps files the team's tests write in the mounted
# checkout owned by a normal user on a Linux host.
RUN useradd --create-home --uid 10001 runner
USER runner

WORKDIR /workspace
