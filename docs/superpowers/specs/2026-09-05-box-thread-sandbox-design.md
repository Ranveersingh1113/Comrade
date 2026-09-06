# Box Thread Sandbox Design

**Date:** 2026-09-05  
**Status:** approved direction; provider proof required before implementation

## Goal

Replace production Docker-socket execution with one persistent ASCII Box per
Comrade thread. A Box supplies a real Linux workspace, dependencies,
long-running processes, snapshots, and optional previews without making
Comrade operate an ECS/EFS sandbox platform.

## Core decision

The existing Comrade thread worktree remains the canonical Git workspace.
Box is the thread's execution environment, not its Git authority:

```
GitHub App -> Comrade local thread worktree -> sanitized archive -> Box
                                           <- command output -- Box
GitHub App <- Comrade local thread worktree <- agent edits / PR capture
```

This preserves the existing GitHub App boundary and pull-request workflow.
No GitHub token, Box API key, model key, database URL, Comrade secret, or
human-uploaded secret enters a Box.

## Box lifecycle

* One Box is associated with one Comrade thread, never a whole team or user.
* Creation uses the Box API with `noEnv: true`, a stable idempotency key, and
  only non-secret `COMRADE_TEAM_ID` / `COMRADE_THREAD_ID` tags.
* Comrade waits for `ready`/`idle` before a command. A `box_starting` response
  is retried as lifecycle state, never treated as a test failure.
* After 15 minutes without an active agent run or detached process, the worker
  calls stop. Box snapshots files and installed dependencies; resume restores
  them. Deletion is reserved for explicit thread/workspace retention cleanup.
* A Box ID, state, timestamps, current detached process ID, and the last
  source fingerprint live in Comrade's database. API keys and preview tokens
  do not.

Box's recommended platform pattern is stop/resume around usage, rather than
permanently running a VM. [Box platform guide](https://docs.ascii.dev/box/platform-guide)

## Repository and dependency sync

Before a Box command, Comrade compares the local worktree fingerprint with the
thread's last uploaded fingerprint. If it changed, the server creates an
archive containing only repository files permitted by current repository
capability rules. It excludes `.git`, `.env`, secret-file patterns, socket/
device files, and symlinks leaving the checkout. The server uploads that
archive through the Box file API, then invokes a fixed unpack command into
`/home/user/comrade-workspace`.

The Box has no GitHub credential and does not clone repositories. Agent edits
remain local, so the existing `capture_patch` and GitHub App approval path
continue unchanged. Test/build side effects in a Box never modify the local
worktree.

Dependencies live outside that unpacked source directory in
`/home/user/.comrade/deps`. Setup runs only from the sync pipeline after the
existing manifest/lockfile checks. A changed lockfile invalidates the remote
dependency state. Normal `repo_run` mounts neither secrets nor Git metadata;
it invokes the existing allowlisted parsed argv in the Box work directory.

The Box file API confines reads/writes to `/home/user` or `/tmp`, which is the
only file-transfer surface Comrade uses. [Box file API](https://docs.ascii.dev/box/api/reference/agent/write-box-file)

## Commands, output, and processes

`repo_run` still accepts one allowlisted command. Comrade parses it locally,
then serializes the parsed argv with safe shell quoting before calling Box's
command endpoint. The model never supplies a Box ID, API request, timeout,
environment map, or arbitrary shell fragment.

Foreground commands keep Comrade's 120-second tool deadline even though Box
permits up to 600 seconds. Setup can use Box's detached-command API and the
worker polls the provider process ID. Output is clipped and datamarked exactly
as it is today. A command submission is never blindly retried: Box documents
that a gateway failure may mean the command already started.

## Credentials and provider configuration

The deployment holds one `COMRADE_BOX_API_KEY`. It is used only by the API/
worker to call `https://ascii.dev/api/box/v1`; it is never uploaded to a Box,
logged, returned to browsers, or stored in Postgres.

The Box account must use an environment marked **safe for third parties** and
Comrade additionally passes `noEnv: true` on every create/fork/resume call.
Box documents that an ordinary environment would otherwise provide GitHub,
agent, secret-file, and Box credentials. [Box environments](https://docs.ascii.dev/box/environments)

## Security boundary and release condition

`noEnv` protects Comrade/account credentials and prevents a Box from acting on
other Boxes. It is not a documented outbound-network allowlist. Box advertises
full Linux VMs and public hosting, so untrusted repository code can potentially
send the repository's own source to the internet.

Therefore Box is not declared production-ready until the provider proof passes:

1. create a `noEnv` Box and prove no account GitHub/agent/Box credentials are
   present;
2. prove its API key cannot access another thread Box;
3. upload, unpack, run, and delete a representative private-repository archive
   without exposing a GitHub token;
4. determine the provider's enforced outbound-egress control. If it does not
   exist, record the explicit accepted risk before inviting private-repository
   teams; and
5. prove stop/resume and delete produce the expected process and filesystem
   lifecycle.

The initial archive transfer is capped by a configurable value and must fail
closed when the provider's file API rejects it. The real provider proof sets
the default cap from observed/documented limits; Comrade does not silently
fall back to injecting a GitHub token into a Box.

## Non-goals

* Do not use Box's `prompt` endpoint or its built-in Codex/Claude agents:
  Comrade keeps its own agent loop, memory, approval, and audit model.
* Do not add preview servers in this first migration. Box supports private
  hosting, but preview URL lifecycle and access controls are a separate user
  feature.
* Do not retain an AWS/Fargate execution backend. Local Docker remains for
  developer tests; Box is the sole production backend after the proof.

## Operator prerequisites

1. Finish Box onboarding as a platform driven by third-party users.
2. Create a Box service API key in the dashboard.
3. Set `COMRADE_BOX_API_KEY` in the server/worker secret store; never paste it
   into chat or commit it to an `.env` file.
4. Confirm the account/environment is marked safe for third parties.

## Sources

* [Box quickstart](https://docs.ascii.dev/box/quickstart)
* [Build a platform on Box](https://docs.ascii.dev/box/platform-guide)
* [Box Public API](https://docs.ascii.dev/box/api/v1)
* [Box commands](https://docs.ascii.dev/box/api/reference/agent/execute-box-command)
* [Long-running tasks](https://docs.ascii.dev/box/long-running-tasks)
