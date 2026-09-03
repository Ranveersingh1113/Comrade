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
# a diff. So this image covers repositories that run on the standard library
# plus the common runners, and anything else gets a clear "not installed"
# rather than a silent wrong answer.
#
# The real fix for that is the shape Codex uses: a setup phase WITH network
# that installs dependencies, then the network off for the run itself. That is
# the next thing to build here, and it is a phase boundary rather than a flag,
# which is why it is not a flag.
FROM python:3.12-slim

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
