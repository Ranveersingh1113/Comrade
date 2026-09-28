# Mumbai pilot migration design

## Goal

Move Comrade's application compute from AWS `eu-north-1` to `ap-south-1`,
where its Supabase database already runs. Preserve correctness, isolation,
secrets, and rollback while reducing ordinary grounded answers from 26–31
seconds toward 10–12 seconds.

Success means:

- the first visible run state reaches the browser within 3 seconds;
- ordinary read-only answers complete within 12 seconds when the model provider
  is healthy;
- HTTPS, Auth, Storage, Realtime, repository execution, and consent continue to
  work;
- the Stockholm host remains a usable rollback until Mumbai is accepted.

## Evidence

The pilot EC2 instance is in Stockholm (`eu-north-1`). Its database connection
uses Supabase's `aws-0-ap-south-1.pooler.supabase.com` endpoint.

A production timing probe measured three grounded turns at 25.7–29.6 seconds.
There were no empty-model retries and the probe bypassed the worker queue. Each
database operation took about 0.73 seconds; a turn performed more than twenty
sequential operations. The two necessary model calls consumed about 10 seconds
combined. The regional database path therefore dominates the removable delay.

## Chosen approach

Use a blue-green host migration. Build Mumbai alongside the live Stockholm
host, verify it through its own hostname, then move callbacks and continuous
deployment. Do not move Supabase and do not redesign the runtime before
remeasuring the co-located system.

The Mumbai host mirrors the current pilot:

- Ubuntu 24.04, x86-64;
- `m7i-flex.large` during migration so capacity does not change with region;
- 30 GiB encrypted `gp3` root volume;
- no SSH key or inbound administration port;
- HTTP and HTTPS public ingress; SSM for administration;
- the existing `ComradePilotSsm` instance profile;
- Docker Engine, Compose, Git, AWS CLI, the sandbox image, and `/opt/comrade`.

The default Mumbai VPC is sufficient for this single-host pilot. A dedicated
VPC, load balancer, autoscaling group, or container orchestrator would not
improve this migration's user-visible result and is outside scope.

## Secret handling

The production environment stays in AWS Secrets Manager at
`comrade/pilot/env` in `eu-north-1`. The instance profile already grants access
to its exact ARN.

The new host downloads the existing `asm-exec` wrapper from the restricted
bootstrap object and resolves the full secret ARN at runtime. Secret values are
written directly to `/opt/comrade/.env` with mode `0600`; they never appear in
chat, SSM output, cloud-init output, or shell tracing.

After the Mumbai public IP is known, the host replaces only the old public
hostname in its local environment. Once acceptance passes, the same runtime
path writes that updated file as a new Secrets Manager version. The prior
version remains available for rollback.

## Provisioning and deployment flow

1. Create a Mumbai security group allowing ports 80 and 443 from the internet.
2. Launch the parallel instance with the existing SSM profile and encrypted
   storage.
3. Wait for SSM registration, then install the host dependencies.
4. Clone Comrade into `/opt/comrade`, materialize `.env` through `asm-exec`, and
   replace the old hostname with the new IP-based `nip.io` hostname.
5. Deploy exact commit `4eca806` with `scripts/deploy_host.sh`.
6. Build `comrade-sandbox:latest` and verify both workers can use the Docker
   socket with the resolved host group ID.
7. Create a production backup and complete a disposable restore drill before
   cutover.

Both hosts may exist during validation. Before user traffic is directed to
Mumbai, stop the Stockholm agent and pipeline workers so only Mumbai drains
shared queues. The Stockholm containers and images remain available for
rollback.

## Public URL and integrations

The public hostname is derived from the allocated Mumbai address using
`nip.io` and recorded in deployment documentation.

Update these settings at cutover:

- `COMRADE_HOST`, frontend API URL, allowed origin, and any preview base URL in
  the production environment;
- Supabase Auth site URL and allowed redirect URLs;
- GitHub App callback, setup, homepage, and webhook URLs that reference the old
  hostname;
- GitHub Actions AWS region and exact SSM instance ID.

Caddy obtains and verifies the new certificate before any callback setting is
changed. Callback changes happen only after the new application passes its
standalone checks.

## Acceptance

Mumbai must pass all of the following before the workflow or external
callbacks move:

- exact Git SHA and six expected Compose services;
- HTTP to HTTPS redirect and trusted TLS certificate;
- `/api/ready` with every mandatory check healthy and `supabase_api: ok`;
- unauthenticated agent request returns 401;
- post-deploy acceptance: three grounded agent answers, Storage upload,
  pipeline parsing, exact fixture cleanup;
- repository dependency installation through the registry proxy, ESM import,
  sandbox execution, and egress-denial checks;
- a temporary production browser account can sign in and complete one streamed
  turn, then is deleted by exact ID;
- database-operation timing falls materially from 0.73 seconds and all three
  representative answers improve. The desired median is at most 12 seconds;
  a result above 15 seconds blocks cutover and triggers another measurement.

After callbacks move, verify one Supabase sign-in redirect and one GitHub App
delivery against Mumbai. Do not create another test pull request merely to
prove the hostname change.

## Cutover and rollback

Cutover order:

1. stop Stockholm workers;
2. update Supabase and GitHub callback URLs;
3. update GitHub Actions to Mumbai and deploy the exact accepted commit once;
4. verify public browser and webhook paths;
5. publish the new pilot URL;
6. update the Secrets Manager environment version and deployment docs.

If any acceptance or callback check fails, restore the old external URLs,
restart Stockholm workers, and leave the workflow pointing at Stockholm. No
database restore is needed because the migration changes no schema or data
location.

Keep Stockholm running until post-cutover acceptance completes. Then stop, but
do not terminate, the instance. Retain its encrypted volume and rollback image
tags until the next successful production release; afterwards snapshot any
needed workspace state and terminate it to end duplicate compute cost.

## Deferred work

Only rework database call batching if co-location still leaves ordinary turns
above the target. Instance right-sizing also waits for Mumbai CPU and memory
measurements. A stable owned domain should replace `nip.io` before broad use,
but it is independent of this latency repair.
