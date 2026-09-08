-- The control plane runs the queue. It does not get to read what is in it.
--
-- 🔴 `comrade_control` held `select (payload)` on public.jobs, and the control
-- role is the ONE role deliberately not scoped to a team — `ctl_jobs` is
-- `using (true)`, because a single sweeper serves every team. So every job
-- payload in the deployment was readable under one cross-team credential.
--
-- That is not theoretical. `enqueue_github_event` queues the whole parsed
-- webhook body: a private repository's pull request descriptions, commit
-- messages, issue and review comments. T21 moved DOCUMENT bytes out of the
-- payload for exactly this reason and left every other payload where it was.
--
-- The claim no longer returns the payload. The worker re-reads it under
-- `comrade_pipeline` scoped to the job's own team, where `pl_jobs` confines it
-- to `current_team()` — the same content, through a role that is allowed to
-- see that team and only that team.
revoke select (payload) on public.jobs from comrade_control;

