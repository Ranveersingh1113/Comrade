# AWS Fargate Sandbox Design

**Date:** 2026-09-05  
**Status:** approved direction; implementation plan pending review

## Goal

Replace the production dependency on a Docker socket with AWS-managed, isolated
Fargate tasks. A repository's code must never receive Comrade credentials,
public inbound access, or public-internet egress during an agent-requested
`repo_run`.

Local Docker remains the development/test runner. It is not a production
fallback: a deployment configured for AWS must fail closed if AWS sandboxing is
unavailable.

## Scope

This change covers the existing finite-command surface only:

* `repo_run` runs one bounded command in an isolated Fargate task.
* repository setup runs in a distinct, higher-risk setup task.
* thread workspaces persist safely between edits and runs.
* the worker records, limits, stops, and audits sandbox tasks.

It deliberately does **not** introduce a new long-running "start server" tool
or preview URL. Comrade has no such tool today. Adding it requires its own
product design: authentication, URL lifecycle, port rules, task persistence,
and user-visible controls. A generic proxy before that surface exists would be
unused security infrastructure.

## Why Fargate

Fargate gives each task an `awsvpc` network interface and dedicated isolated
compute. The AWS control plane, not the Comrade worker, starts/stops tasks.
This removes the worker's dangerous requirement for a Docker daemon/socket.

CodeBuild remains suitable for CI-like setup jobs, but not as Comrade's
workspace runtime: it does not model a thread-owned workspace and a task
lifecycle that the harness controls. No third-party sandbox provider is
introduced while Comrade is already deployed in AWS.

## Architecture

```
agent/worker -- EFS full workspace mount --> thread checkout
                                               |
                                               | access point, one per thread
                                               v
                                     Fargate run task (/workspace)
                                     no task role; no public route

sync job --> Fargate setup task --> EFS /deps (same thread access point)
                  |
                  +--> egress proxy --> approved package registries only
```

### Workspace isolation

The worker stores workspaces on encrypted EFS instead of host disk in AWS.
Each thread gets:

* a dedicated EFS directory;
* an EFS access point rooted at that directory;
* POSIX identity UID/GID `10001`, matching the sandbox runner; and
* a recorded access-point ID owned by Comrade's control plane.

The execution task mounts only that access point at `/workspace`. It cannot
walk to another thread or team through the EFS mount. The worker has the
separate, privileged full EFS mount needed to create a checkout and apply
agent edits; untrusted tasks never receive it.

An access point is retained while its thread workspace is retained, then
deleted only after the workspace and its task records are confirmed gone.
The deletion path is idempotent. An unknown/missing access point makes a run
fail; it must never fall back to mounting the EFS root.

### Run task: agent-requested commands

Each `repo_run` creates one Fargate task with:

* `awsvpc` networking in a private run subnet with no `0.0.0.0/0` route;
* no public IP and a security group with no ingress and no outbound internet;
* EFS mounted through the thread access point;
* read-only container root filesystem, a bounded writable temp filesystem, and
  non-root UID/GID `10001`;
* 0.5 vCPU, 1 GiB memory, 256 processes-equivalent task quota, 120-second
  command deadline, and an explicit stop after timeout;
* no task IAM role, so code in the container receives no AWS credentials;
* only an execution role, used by ECS outside the container to pull ECR images
  and write audit logs; and
* an immutable sandbox image from ECR, pinned by digest rather than `latest`.

The run subnet has only the private VPC endpoints required for ECS operation:
ECR API, ECR Docker, CloudWatch Logs, and S3 gateway. Those endpoints let the
ECS agent fetch an image and publish logs; they do not grant the repository's
process public internet access.

The worker receives command output through CloudWatch Logs, clips and
datamarks it exactly as `agent.sandbox.run_contained` does today, and records
the ECS task ARN, exit status, start/finish times, and timeout outcome.

### Setup task: dependency preparation

Dependency installation is arbitrary code execution and cannot have the same
network policy as `repo_run`. It stays unreachable from the model and runs only
from the repository-sync pipeline after manifest/lockfile validation.

It gets the same EFS access point and base hardening, but uses a separate task
definition and isolated setup subnet. Its only route is an egress proxy that
allows the existing declared registries:

* `pypi.org`, `files.pythonhosted.org`
* `registry.npmjs.org`
* `proxy.golang.org`, `sum.golang.org`
* `crates.io`, `static.crates.io`

The proxy is the enforced boundary, not `HTTP_PROXY` environment variables.
The task security group and route table permit no bypass path. The setup task
uses no task role and receives no Comrade, database, model, or GitHub secret.
Its resulting dependency directory is read-only to later run tasks.

The setup subnet, proxy, and EFS are a distinct cost/security unit. If the
proxy cannot be reached or policy enforcement cannot be established, setup
fails with an actionable environment status; it never quietly uses open
egress.

### Control plane and IAM

The existing `comrade_control` database role remains the application-side
control plane. AWS adds an application deployment role with only the actions
needed to create/read/stop tasks from the fixed sandbox task-definition family
and to create/delete the EFS access points for workspace records. It cannot
pass arbitrary roles or register arbitrary task definitions.

The ECS **execution role** has only ECR image pull and CloudWatch log write
permissions. It is not a task role. The sandbox task definition intentionally
omits `taskRoleArn`, Secrets Manager injection, ECS Exec, and environment
credentials. That preserves the codebase rule that untrusted code never gets
platform credentials.

### Per-thread task-definition revisions

ECS stores an EFS access-point ID in a task definition; `RunTask` can override
the command/environment but not an EFS volume. Consequently, a static task
definition cannot safely mount a different thread root for each run.

CloudFormation creates and protects the reviewed task-definition **template**
(image digest, runner user, logging, network mode, execution role, resource
limits, and no task role). The control plane copies that template into a
thread-bound run or setup revision only after creating/reusing the two recorded
EFS access points. Its only variable fields are the two access-point IDs and
their read-only mount modes. It launches that exact revision, never a family
name or "latest" revision. When the workspace is deleted and no task references
it, the control plane deregisters its revisions and deletes its access points.

The control-plane deployment role is narrowly permitted to register only the
two known sandbox families and to pass only the fixed execution role to ECS.
It cannot choose an image, task role, cluster, or arbitrary family from model
input. Registration input is assembled from an immutable application template,
not accepted from the agent/tool request.

IAM policy actions and resource conditions will be generated from the actual
AWS SDK calls during implementation and verified against AWS's service
authorization reference; they will not be hand-waved from this document.

### Task lifecycle and limits

A new sandbox-task record binds an AWS task ARN to team, thread, run ID,
workspace access point, phase (`setup` or `run`), state, deadlines, and
observed exit result. Database constraints ensure a task belongs to one thread
and only valid state transitions are accepted.

Worker behaviour:

1. reserve the existing team quota before launch;
2. create/reuse the thread workspace access point;
3. launch exactly the fixed task definition for the selected phase;
4. wait only to the phase deadline;
5. call `StopTask` on timeout/cancellation, then reconcile status;
6. release quota once a terminal outcome is persisted; and
7. sweep abandoned tasks and expired workspaces on worker startup and on a
   schedule.

The launch request includes an idempotency token tied to the agent run and
phase. Replaying a job cannot create a second live sandbox for the same run.

## Configuration

AWS production requires these explicit settings:

* `COMRADE_SANDBOX_BACKEND=fargate`
* AWS region, ECS cluster, run/setup task-definition template ARNs, run/setup
  subnet IDs, run/setup security group IDs, EFS filesystem ID, EFS root mount
  path, CloudWatch log group, and ECR image digest
* maximum concurrent tasks per team and a global task cap

`COMRADE_SANDBOX_BACKEND=docker` is allowed only in development/test.
Production config validation rejects Docker and rejects a missing AWS value.
The code will not infer account IDs, subnets, or task definitions from defaults.

## Failure behaviour

* ECS launch, EFS access-point, image-pull, or log-read failures return a
  sandbox infrastructure error; they do not masquerade as a failed test.
* A stopped/timed-out task is not a successful verification.
* Missing output after a terminal ECS success is an infrastructure error, not
  an empty test result.
* A setup failure marks its environment failed/partial exactly as today; run
  never mounts a partial dependency directory.
* Worker restart reconciliation queries persisted live task ARNs before
  releasing quotas or deleting a workspace.

## Security invariants and tests

The implementation must prove, with unit tests plus a real AWS staging lane:

1. run tasks have no public route, public IP, task role, or secrets;
2. run-task output cannot reach arbitrary public internet/DNS;
3. setup traffic reaches an allowed registry and a disallowed host is blocked;
4. a task mounted through thread A's access point cannot list/read thread B;
5. task timeout/cancel invokes `StopTask` and persists a terminal status;
6. repeated delivery of one job launches at most one task;
7. container-visible environment lacks all Comrade and AWS credentials; and
8. expired workspace sweep deletes the EFS directory and access point only
   after no live task references them.

The real AWS lane is mandatory before declaring the AWS backend production
ready. Local mocks prove request construction and failure handling only; they
cannot prove VPC routing, EFS isolation, or credential absence.

## Delivery order

1. Infrastructure and configuration validation: VPC subnets/endpoints,
   encrypted EFS, ECR image, fixed task definitions, roles, logging, alarms.
2. Workspace access-point lifecycle and persistent task records.
3. Fargate `repo_run` backend with task lifecycle and output collection.
4. Separate setup backend plus enforced egress proxy.
5. AWS staging/adversarial lane and operations runbook.

Each stage is independently testable. Docker is retained locally until stage 5
passes, then rejected by production configuration rather than silently used.

## Sources

* [Amazon ECS/Fargate task networking](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-tasks-services.html)
* [Private Fargate connectivity requirements](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/vpc-endpoints.html)
* [EFS volumes for ECS](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/efs-volumes.html)
* [EFS access points in ECS task definitions](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/specify-efs-config.html)
* [CodeBuild proxy network pattern](https://docs.aws.amazon.com/codebuild/latest/userguide/use-proxy-server-transparent-components.html)
