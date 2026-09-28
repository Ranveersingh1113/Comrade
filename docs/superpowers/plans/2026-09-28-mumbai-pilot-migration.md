# Mumbai Pilot Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the Comrade pilot compute to AWS Mumbai and cut production over only after the co-located stack proves faster and fully functional.

**Architecture:** Provision a parallel SSM-managed EC2 host in `ap-south-1`, materialize the existing Secrets Manager environment through `asm-exec`, and validate it through a new `nip.io` hostname. Move external callbacks and GitHub Actions after acceptance; retain the stopped Stockholm instance and volume for rollback.

**Tech Stack:** AWS EC2, IAM, SSM, Secrets Manager, S3, EBS, Docker Compose, Caddy, GitHub Actions, hosted Supabase, pytest, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-28-mumbai-pilot-migration-design.md`

## Global Constraints

- Mumbai region is `ap-south-1`; Supabase remains unchanged.
- Use `m7i-flex.large`, Ubuntu 24.04 x86-64, and a 30 GiB encrypted `gp3` root volume.
- Open only ports 80 and 443; administer with SSM and create no SSH key.
- Never fetch or print secret values. Resolve the full existing secret ARN only through `asm-exec`.
- Deploy exact commits and preserve migration-before-activation ordering.
- Do not stop Stockholm workers until Mumbai passes standalone acceptance.
- A median ordinary answer above 12 seconds is undesirable; any representative answer above 15 seconds blocks cutover.
- Stop rather than terminate Stockholm after cutover; retain its encrypted volume.

## Review Focus

- A secret or environment value reaches SSM/cloud-init output: bootstrap must fail closed and logs must contain references only.
- Supabase or GitHub still redirects to Stockholm: complete a real temporary browser sign-in and inspect one GitHub delivery before cutover completes.
- Both regions drain queues during cutover: verify Stockholm workers stopped and Mumbai workers live before publishing the URL.
- GitHub Actions targets a mismatched region and instance: a repository test must pin both values together.
- Mumbai fails after callbacks move: restore old callback URLs and restart Stockholm workers without a database restore.

---

### Task 1: Capture rollback state and production baseline

**Files:**
- No repository changes.

**Interfaces:**
- Consumes: live Stockholm instance `i-0e5e97d751ffbd262`, deployed commit `4eca8062bfa0574407fee69ce5ab4742ebb34c16`.
- Produces: verified backup path, Stockholm image tags, and baseline timings used by Task 4.

- [ ] **Step 1: Verify the current live boundary**

Run public HTTPS, `/api/ready`, unauthenticated agent, exact Git SHA, Compose service, and worker-capability checks.

Expected: HTTPS 200, readiness 200 with all mandatory checks `ok`, unauthenticated agent 401, exact SHA `4eca806...`, and six services running.

- [ ] **Step 2: Create and restore-verify a fresh production backup**

Run `python -m scripts.backup` from the production `migrate` service into a timestamped `/var/backups/comrade/pre-mumbai-*` directory and execute its restore drill.

Expected: roles, policies, tables, migrations, teams, and RLS counts all verify; command exits 0.

- [ ] **Step 3: Tag the running Stockholm images for rollback**

Tag API, frontend, both workers, registry proxy, and sandbox images with `rollback-4eca806` and record the immutable image IDs.

Expected: all six rollback tags resolve locally and no container restarts.

### Task 2: Provision the Mumbai host

**Files:**
- No repository changes.

**Interfaces:**
- Consumes: global instance profile `ComradePilotSsm` and the default Mumbai VPC.
- Produces: `MUMBAI_INSTANCE_ID`, `MUMBAI_PUBLIC_IP`, `MUMBAI_HOST`, security-group ID, and root-volume ID for every later task.

- [ ] **Step 1: Resolve the regional Ubuntu image**

Read Canonical's public SSM parameter for the current Ubuntu 24.04 amd64 gp3 AMI in `ap-south-1` and verify the owner is Canonical (`099720109477`).

Expected: one non-deprecated `ami-*` value.

- [ ] **Step 2: Create the web security group**

Create `ComradePilotWeb` in Mumbai's default VPC with ingress TCP 80/443 from IPv4 and IPv6, default outbound access, and no SSH ingress.

Expected: `describe-security-groups` shows exactly the intended inbound ports.

- [ ] **Step 3: Launch the instance**

Launch one `m7i-flex.large` with the `ComradePilotSsm` profile, no key pair, encrypted 30 GiB gp3 root disk, public IP, and tags `Name=Comrade-pilot-mumbai`, `Project=Comrade`, `Environment=pilot`.

Expected: the instance reaches `running`, status checks pass, and SSM reports `Online`.

- [ ] **Step 4: Derive and record the hostname**

Convert the public IPv4 address to its dashed `nip.io` hostname and retain all produced IDs in the active migration log without committing them yet.

Expected: DNS resolves to the new public IP.

### Task 3: Bootstrap secrets and runtime without exposing values

**Files:**
- No repository changes.

**Interfaces:**
- Consumes: Task 2 host identifiers, S3 object `comrade-bootstrap-276833795981-eun1/asm-exec`, and full Secrets Manager ARN `arn:aws:secretsmanager:eu-north-1:276833795981:secret:comrade/pilot/env-lyjM6H`.
- Produces: ready `/opt/comrade` checkout, mode-0600 Mumbai `.env`, Docker runtime, and sandbox image.

- [ ] **Step 1: Install host dependencies through SSM**

Install Docker Engine, Compose, Git, AWS CLI, and required certificates; enable Docker; create `/opt/comrade` and `/workspaces`; add the Ubuntu user to Docker.

Expected: `docker version`, `docker compose version`, `git --version`, and SSM command all succeed.

- [ ] **Step 2: Install `asm-exec` from the restricted bootstrap object**

Download only the allowed S3 key, install it mode `0755`, and verify its checksum/usage without resolving a secret.

Expected: executable exists and no secret call appears in output.

- [ ] **Step 3: Clone the repository at the accepted commit**

Clone into `/opt/comrade`, fetch `4eca8062bfa0574407fee69ce5ab4742ebb34c16`, and checkout detached.

Expected: `git rev-parse HEAD` equals the accepted SHA and the worktree is clean.

- [ ] **Step 4: Materialize and retarget the environment**

Use `asm-exec` with the full secret ARN to write `/opt/comrade/.env` under `umask 077`. In the same non-logging child process, replace every exact occurrence of `13-62-11-26.nip.io` with `MUMBAI_HOST`.

Expected: file mode is `0600`; a validation command reports required variable names and the new hostname without printing values.

- [ ] **Step 5: Build the sandbox image**

Build `comrade-sandbox:latest` from `docker/sandbox.Dockerfile` and resolve the host Docker group ID for Compose.

Expected: sandbox Python, Node, npm, pnpm, and Docker CLI versions match the existing acceptance checks.

### Task 4: Deploy and accept Mumbai independently

**Files:**
- No repository changes.

**Interfaces:**
- Consumes: Task 3 checkout/environment and existing release scripts.
- Produces: standalone accepted Mumbai deployment and measured latency evidence.

- [ ] **Step 1: Deploy the exact accepted commit**

Run `scripts/deploy_host.sh 4eca8062bfa0574407fee69ce5ab4742ebb34c16` through SSM.

Expected: migrations complete, readiness succeeds within the 30-second probe budget, and public proxy verification exits 0.

- [ ] **Step 2: Verify host and public boundaries**

Check exact SHA, six services, trusted TLS, HTTP 308, root 200, `/api/ready` 200, and unauthenticated agent 401.

Expected: every check passes against `MUMBAI_HOST`.

- [ ] **Step 3: Run product acceptance**

Run `scripts.post_deploy_check`, repository installation/execution, ESM resolution, sandbox escape refusals, and registry-proxy allow/deny checks.

Expected: three grounded answers, Storage ingestion and teardown pass; no leftover verification containers, networks, volumes, rows, or objects.

- [ ] **Step 4: Measure co-located latency**

Run the same throwaway timing probe used for the Stockholm baseline against three representative read-only questions, emit durations only, then delete it.

Expected: database operations fall materially below 0.73 seconds; every answer is grounded and at most 15 seconds. If not, stop before cutover.

### Task 5: Move external URLs and verify browser integrations

**Files:**
- No repository changes.

**Interfaces:**
- Consumes: accepted `MUMBAI_HOST` from Task 4.
- Produces: Supabase Auth and GitHub App settings pointing to Mumbai, with exact rollback values recorded.

- [ ] **Step 1: Stop the Stockholm worker fleet**

Stop only `agent-worker` and `pipeline-worker`, wait past the heartbeat stale threshold, and verify Mumbai readiness still reports both worker kinds live. Restart them immediately if any later Task 5 check fails.

Expected: Mumbai is the only region draining shared queues before callbacks move.

- [ ] **Step 2: Update Supabase Auth URLs**

Set the site URL and allowed redirects to the Mumbai HTTPS origin while retaining the Stockholm origin until final verification.

Expected: configuration visibly contains both origins during the rollback window.

- [ ] **Step 3: Update GitHub App URLs**

Change homepage, callback/setup, and webhook URLs that reference Stockholm to their Mumbai equivalents; retain the old values in the migration record.

Expected: GitHub App settings display the new HTTPS URLs.

- [ ] **Step 4: Verify browser sign-in and streaming**

Create one synthetic production user by exact ID, sign in through the deployed browser, send one read-only turn, verify streamed progress/final output, then delete that exact account and rows.

Expected: redirect returns to Mumbai, first visible state is within 3 seconds, final answer is grounded, cleanup count is zero.

- [ ] **Step 5: Verify GitHub delivery**

Inspect or redeliver one non-destructive GitHub App webhook and confirm a 2xx response from Mumbai. Do not create a test pull request.

Expected: delivery targets the Mumbai URL and records success.

### Task 6: Pin continuous deployment to Mumbai

**Files:**
- Modify: `.github/workflows/deploy-pilot.yml`
- Modify: `tests/test_deploy_host_script.py`
- Modify: `docs/deployment.md`
- Modify: `fix.md`

**Interfaces:**
- Consumes: exact Mumbai region, instance ID, hostname, and acceptance evidence.
- Produces: repository-controlled future deployments to Mumbai.

- [ ] **Step 1: Write the failing workflow-target test**

Add `test_workflow_targets_the_accepted_mumbai_host()` asserting the workflow contains `aws-region: ap-south-1`, the exact `MUMBAI_INSTANCE_ID`, and no Stockholm instance ID.

- [ ] **Step 2: Run the focused test and verify failure**

Run: `uv run pytest -q tests/test_deploy_host_script.py::test_workflow_targets_the_accepted_mumbai_host`

Expected: FAIL because the workflow still names Stockholm.

- [ ] **Step 3: Update the workflow and documentation**

Change only the workflow region/instance target. Update `docs/deployment.md` with Mumbai IDs/URL and measured latency; add the region-mismatch finding and accepted evidence to `fix.md`.

- [ ] **Step 4: Run focused and canonical gates**

Run the deployment-script suite, then `scripts/gates.sh` with the local Supabase service key scoped only to the gate process.

Expected: focused tests pass; backend, frontend, integration, and browser lanes all pass.

- [ ] **Step 5: Commit and push to `master`**

Stage only the workflow, test, docs, ledger, spec, and plan. Commit with `fix: move pilot compute beside Supabase`, then push exact HEAD to `master`.

Expected: GitHub Actions deploys the exact commit to Mumbai and succeeds.

### Task 7: Complete cutover and retain rollback

**Files:**
- Modify locally after acceptance: `fix.md`

**Interfaces:**
- Consumes: successful Mumbai workflow run and callback verification.
- Produces: Mumbai as the sole active worker fleet, updated secret version, and stopped Stockholm rollback host.

- [ ] **Step 1: Repeat post-cutover acceptance**

Verify browser sign-in/streaming, Storage ingestion, GitHub delivery, sandbox execution, and three representative answer timings against the workflow-deployed SHA.

Expected: every check passes with no fixture residue.

- [ ] **Step 2: Persist the Mumbai environment version safely**

From Mumbai, use `asm-exec` to materialize the current secret, apply only the accepted hostname replacement, and submit the file as a new Secrets Manager version without printing it.

Expected: Secrets Manager reports a new `AWSCURRENT` version; `/api/ready` remains healthy.

- [ ] **Step 3: Stop Stockholm and verify rollback assets**

Stop, do not terminate, `i-0e5e97d751ffbd262`. Verify its encrypted 30 GiB volume remains attached and the instance can be started by ID.

Expected: Mumbai remains healthy after Stockholm stops and no duplicate EC2 compute remains running.

- [ ] **Step 4: Record final evidence**

Record workflow URL, exact SHA, Mumbai instance/IP/hostname, backup path, acceptance results, and before/after timings in `fix.md` at the next code-bearing release rather than triggering a documentation-only deployment.

Expected: no uncommitted infrastructure-target changes remain; unrelated local generated files stay untracked.
