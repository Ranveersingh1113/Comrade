-- Team-visible open COUNTS per document ("opened by 3 of 4").
--
-- document_opens RLS is deliberately per-viewer (who has/hasn't opened a doc
-- routes privately as nudges — never shown to whoever asks). A count doesn't
-- name anyone, so it can be team-visible without breaking the no-guilt
-- invariant. That requires aggregating across rows the caller cannot SELECT,
-- hence a definer view (owner's rights) whose own WHERE clause re-applies the
-- team boundary. It exposes nothing row-level: ids and counts only.

create view public.document_opens_summary
with (security_invoker = off) as
select
  d.id                                as document_id,
  d.team_id,
  count(o.id)                         as opens_count,
  (select count(*) from public.memberships m
    where m.team_id = d.team_id and m.status = 'active') as member_count
from public.documents d
left join public.document_opens o on o.document_id = d.id
where public.is_team_member(d.team_id)   -- caller-bound: auth.uid() inside
  and d.deleted_at is null
group by d.id, d.team_id;

grant select on public.document_opens_summary to authenticated;
