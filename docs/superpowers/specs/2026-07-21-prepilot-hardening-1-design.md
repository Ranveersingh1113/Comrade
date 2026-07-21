# Pre-pilot hardening, slice 1 — Design

Date: 2026-07-21 · Status: approved (owner, in-session)
Closes the top of the production-readiness punch list: two invariant
violations reachable with one curl, and the unmetered LLM spend hole.

## Hole 1 — Storage bucket does not exist, storage RLS absent

`frontend/src/screens/Documents.tsx:60` uploads to `storage.from('documents')`.
No migration or config provisions that bucket, and **no policies on
`storage.objects` exist at all**. On a fresh environment every upload fails;
hand-creating the bucket without policies makes team documents world-readable
to any authenticated user. Postgres RLS is meticulous; Storage RLS is missing.

**Fix — one migration:**
- Private bucket `documents` (`insert into storage.buckets ... on conflict do nothing`).
- Two policies on `storage.objects` for `authenticated`, reusing the existing
  `public.is_team_member()` helper, keyed on the first path segment. Upload
  paths are already `{teamId}/{uuid}-{filename}`, so
  `((storage.foldername(name))[1])::uuid` is the team id.
- SELECT + INSERT only. The app never deletes or updates objects — document
  deletion is soft (`documents.deleted_at`), files remain.
- No frontend change: reads already go through `createSignedUrl`
  (`Documents.tsx:114`), which is exactly what a private bucket requires.

## Hole 2 — Any member can silently kill memory diff cards

`POST /observations/{id}/suppress` checks `sender_kind='ai'` and
`thread_type='group'` but not whether the message is a compilation's
`diff_message_id`. The frontend hides the button on diff cards; the API does
not. One curl tombstones the team's memory-change notifications — breaking
the post-hoc transparency that the whole no-approval-gate memory model rests
on ([[project-comrade]]: "not an approval request — a notification").

**Fix —** add to the existing SELECT inside the suppression insert:
```sql
and not exists (
  select 1 from public.memory_compilations c where c.diff_message_id = m.id
)
```
The endpoint already 404s when the select returns no row. No new code path.

## Hole 3 — `/agent/turn` is an unmetered money faucet

No rate limiting anywhere on the HTTP surface. Every turn is a synchronous
Gemini call. A loop from any authenticated member runs up the API bill and,
because turns block threadpool workers for seconds, freezes the API for
everyone.

**Fix —** `agent_runs` already records `team_id` and `started_at` for every
turn, so the rate limit is a count query against a table that exists:

```sql
select count(*) from public.agent_runs
 where team_id = %s and started_at > now() - interval '1 hour'
```

Reject with **429** when the count is at or above the cap. No new table, no
new dependency, correct across multiple API instances (shared DB), survives
restart.

- Config: `AGENT_TURNS_PER_HOUR`, default 60.
- Applied to `POST /agent/turn` only — the only endpoint that costs money.
  Other endpoints are cheap RLS'd DB writes.
- Checked after `require_membership` (a non-member must get 403, not 429 —
  no leaking that a team exists or how busy it is).

**Deliberate scope calls:**
- **Team-scoped, not per-member.** Cost is the blast radius worth capping.
  One member monopolising a 4-person team's budget is a social problem.
- **Turn count, not tokens.** True token accounting needs response-metadata
  plumbing through the ADK runner. Turn count is the honest proxy at pilot
  scale.

## Testing

One test per hole, in the existing suites:
1. A member can insert into their own team's storage folder; a non-member
   cannot insert into it. (integration layer, real stack)
2. Suppressing a compilation's diff message returns 404 and leaves the
   message undeleted. (pytest)
3. `/agent/turn` returns 429 once the hourly cap is reached, and the 403
   non-member path still wins over 429. (pytest)

## Explicitly skipped

| Skipped | Add when |
|---|---|
| Per-member sub-limit | a team reports one member eating the budget |
| Real token accounting | the turn-count proxy misprices actual spend |
| Rate limits on other endpoints | abuse appears there |
| Retry-After header / backoff UX | members actually hit the cap in practice |
