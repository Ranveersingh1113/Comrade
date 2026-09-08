-- One team's backlog must not become every other team's outage.
--
-- 🔴 The pipeline claim was `order by created_at` across the WHOLE queue, with
-- no per-team consideration of any kind. A team that connects a busy
-- repository queues one job per webhook delivery and one per document, and
-- every one of those is older than the next team's first job — so one team's
-- backlog sat at the head of the queue and everybody else waited behind it.
-- T12 gave agent turns a per-team ceiling for exactly this reason and left
-- this queue, whose depth is driven by an EXTERNAL event rate rather than by
-- members typing, first-come-first-served.
--
-- The claim now orders by the team served longest ago (a team never served
-- sorts first), then oldest-first within that team. This index makes "when was
-- this team last served" a lookup rather than a scan of its job history.
create index if not exists idx_jobs_team_served
  on public.jobs (team_id, picked_at desc)
  where picked_at is not null;
