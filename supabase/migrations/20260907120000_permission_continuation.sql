-- A run waiting on a person is not a crashed worker.
--
-- 🔴 `pause_for_permission` parked a run as `waiting_for_permission` and left
-- its WORKER LEASE running. That lease answers one question — is the process
-- holding this run still alive — and it is measured in minutes because a dead
-- worker should be noticed quickly. A human deciding whether to approve an
-- action takes minutes to days.
--
-- So `recover_expired_agent_runs` found the waiting run, called it abandoned,
-- requeued it; the re-run proposed the same action, hit the same pending card,
-- and parked again. Three cycles later the run was `failed: worker lease
-- expired` while the card sat there pending and the member had done nothing
-- wrong. Approving it then executed the action against a run already declared
-- dead, and `_requeue_permission_run` — which matches only
-- `status='waiting_for_permission'` — resumed nothing.
--
-- The lease is now cleared when a run parks (shared/agent_runs.py), so
-- recovery no longer sees it. The backstop becomes the CARD's own expiry,
-- which is the clock that actually measures the thing being waited on.

-- Widening only: an unanswered card is not `cancelled` — nobody decided
-- anything — and calling it that would put a decision in someone's mouth.
alter table public.consent_queue drop constraint if exists consent_queue_status_check;
alter table public.consent_queue add constraint consent_queue_status_check check (
  status in (
    'pending', 'approved', 'edited', 'cancelled', 'executed', 'rejected',
    'expired'
  )
) not valid;
alter table public.consent_queue validate constraint consent_queue_status_check;

create or replace function public.recover_expired_agent_runs()
returns integer language plpgsql security definer set search_path = '' as $$
declare _count integer; _expired integer;
begin
  -- Runs whose WORKER vanished. A parked run has no lease and no worker, so
  -- it is not in scope here any more — which is the point.
  update public.agent_runs
  set status = case when attempts >= 3 then 'failed' else 'queued' end,
      worker_id = null, lease_expires_at = null,
      finished_at = case when attempts >= 3 then now() else null end,
      last_error = coalesce(last_error, 'worker lease expired')
  where status in ('running', 'waiting_for_permission', 'waiting_for_user')
    and lease_expires_at < now();
  get diagnostics _count = row_count;

  -- Cards nobody answered before their seven-day backstop ran out.
  update public.consent_queue
  set status = 'expired', resolved_at = now(),
      resolution_reason = coalesce(
        resolution_reason, 'nobody answered this before it expired'
      )
  where status = 'pending' and expires_at < now();
  get diagnostics _expired = row_count;

  -- And the runs those cards were holding open. Narrow on purpose: a run is
  -- ended only when one of its cards actually EXPIRED and none is still
  -- answerable. Failing every parked run with no pending card would race the
  -- gap between approving a card and requeueing its run.
  update public.agent_runs r
  set status = 'failed', finished_at = now(),
      worker_id = null, lease_expires_at = null,
      last_error = coalesce(
        r.last_error, 'the approval this turn was waiting for expired unanswered'
      )
  where r.status = 'waiting_for_permission'
    and exists (
      select 1 from public.consent_queue c
      where c.agent_run_id = r.id and c.status = 'expired'
    )
    and not exists (
      select 1 from public.consent_queue c
      where c.agent_run_id = r.id and c.status = 'pending'
    );

  return _count + _expired;
end;
$$;
