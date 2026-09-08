-- Queue cleanup for a container the row could only NAME.
--
-- 🔴 (fix.md F07) `sandbox_processes` reserves the container name BEFORE the
-- launch and writes the id back after it, precisely so a worker that dies in
-- between leaves evidence of what to look for. This trigger then filtered on
-- `old.container_id is not null` — so deleting the thread dropped the row, the
-- name and the only handle on a container that was still running.
--
-- That is the exact leak sandbox_cleanup was created to prevent, arriving
-- through the one window the table's own design had anticipated.
create or replace function public.trg_sandbox_process_cleanup()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  -- Only what actually needs reclaiming. A process that already stopped has
  -- nothing running, and queueing it would make the reconciler chase ghosts.
  --
  -- Either handle is enough to reclaim by: the drain prefers the id and falls
  -- back to the name, the same order every other reclaimer uses.
  if (old.container_id is not null or old.container_name is not null)
     and old.state in ('starting', 'running') then
    insert into public.sandbox_cleanup
      (container_id, container_name, network, reason)
    values (old.container_id, old.container_name,
            'comrade-prev-' || replace(old.id::text, '-', ''),
            'owning thread or team was deleted');
  end if;
  return old;
end;
$$;
