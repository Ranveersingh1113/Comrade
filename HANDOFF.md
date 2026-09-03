# Comrade — Frontend Handoff

> Written 2026-07-17 for the session starting frontend work. Backend state: everything below
> is merged to local `master` (⚠️ **~14 commits ahead of `origin/master`, unpushed**).
> If you run Claude in this project directory, project memory auto-loads with richer context;
> the durable docs are in `docs/` (see "Reading list" at the bottom).

---

## 1. What you're building the frontend for

**Comrade** = an AI teammate for **teams without a manager** (student teams are the pilot beachhead;
startups/informal teams are the tail). The AI is a *silent member* of a team room: it reads
everything, compiles a citable team wiki, surfaces coordination gaps, and takes actions only
through a peer-consent protocol. Flat governance — no admin role, no approval hierarchy — is
**the product**, not a missing feature.

Product invariants the UI must never violate:
- **No rankings, no leaderboards, no activity feeds.** Contribution = per-member task bar + GitHub signals only.
- **Quiet/inactivity signals are never shown to whoever asks** — they route privately to the person concerned (nudges).
- **Private AI threads are invisible to everyone** including leaders. Group room is shared.
- **The AI acts as itself** — never attributed to the member who triggered it. Voice: warm, 1–2 sentences, no guilt, no blame, no emojis.
- **Deletion leaves a trace**: "delete for everyone" renders a placeholder, not a silent gap.

## 2. Architecture in one screen

```
Frontend (your work)
  ├─ supabase-js: Auth (user JWT) + direct table reads/writes under RLS  ← most UI data
  ├─ Supabase Realtime: postgres_changes subscriptions (RLS-scoped)      ← live updates
  └─ HTTP → FastAPI agent runtime: POST /agent/turn                      ← talk to the AI

Backend (built)
  ├─ server/app.py         FastAPI: POST /agent/turn, GET /health
  ├─ agent/                ADK LlmAgent (Gemini 2.5 Flash), 19 tools, propose-only
  │                        + capability layer and container sandbox
  ├─ pipeline/             compiler (two-stage), wiki projection, chat→memory, job worker
  └─ shared/               db roles (RLS), consent engine, nudges, config
```

**Security model (why the frontend talks to Postgres directly):** every table has RLS.
End users run as `authenticated` scoped by `auth.uid()`; the policies in
`supabase/migrations/20260612095500_rls.sql` (+ later migrations) define *exactly* what a member
can see/do — the frontend needs no permission logic of its own, and must not try to bypass it.
Backend workers use separate Postgres roles (`comrade_agent` propose-only, `comrade_executor`,
`comrade_pipeline` sole memory writer). Never use `service_role` anywhere in the frontend.

## 3. API surface (the honest version)

**Every endpoint below except `/health` requires `Authorization: Bearer <supabase session access_token>`.**
Identity (`requester_id`) comes from the verified JWT — never from the body. `team_id` is still a body
field (you belong to many teams, so it is routing, not identity), but every endpoint re-checks your
membership through your *own* RLS context, so naming a team you're not in gets a 403.

| Endpoint | Status | Notes |
|---|---|---|
| `POST /agent/turn` `{team_id, text, thread_type}` → `{run_id, reply, user_message_id, reply_message_id}` | ✅ built | Persists both the member's message and the AI reply to `messages`; let Realtime deliver them rather than double-inserting client-side. `thread_type` is `private` (default) or `group`. Returns **429** when the team is over its hourly turn cap (`AGENT_TURNS_PER_HOUR`, default 60). |
| `POST /agent/turn/stream` — same body | ✅ built | Newline-delimited JSON: `run` → `tool_call`/`text` → `done` (or `error`). Use `fetch` + `ReadableStream`, **not `EventSource`** (it cannot send `Authorization`). 403/429 arrive as real statuses before the stream opens. |
| `POST /consent/{id}/approve` `{team_id}` | ✅ built | Approves **and executes**. Authorisation is RLS (`au_consent_queue_update`: requester only) → 404 if not yours. |
| `POST /consent/{id}/reject` `{team_id}` | ✅ built | |
| `POST /consent/{id}/edit_and_approve` `{team_id, args}` | ✅ built | Re-stamps the action hash, then executes. |
| `POST /documents/{id}/ingest?team_id=…` (multipart `file`) | ✅ built | Members can't write `jobs`; this is the bridge. Insert the `documents` row under RLS first. |
| `GET /health` | ✅ built | |
| Everything else | **direct Supabase** | tables + Realtime + Storage via supabase-js under RLS |

Read consent state from the `consent_queue` table (RLS-scoped, Realtime-subscribable); use the
endpoints only to *act*.

Run it: `uv run uvicorn server.app:app --reload` (port 8000). Needs `SUPABASE_JWT_SECRET`
(from `npx supabase status`) and `CORS_ORIGINS` in `.env` — see `.env.example`.

## 4. What a member can do via RLS (drives every screen)

From the `au_*` policies — this IS the frontend's permission matrix:

- **profiles**: read self + anyone you share a team with; edit self.
- **teams**: members read; anyone creates (as creator); **leader** renames/deletes.
- **memberships**: members see roster; leader invites/removes; you accept your own invite.
- **messages**: group thread — members read + post (as themselves); private thread — owner only.
  AI messages arrive via backend (`sender_kind='ai'`). Edit own messages (deletion = set `deleted_scope`).
- **documents**: members read/upload/edit (soft-delete via `deleted_at`).
- **memory_*** (pages/entries/versions/citations/compilations): **read-only** for members.
- **memory_reverts**: members **insert their own** — this is the one-tap revert (write `entry_id` + own `member_id`).
- **tasks / milestones**: any member creates/edits. Task lifecycle: `proposed → confirmed → in_progress → done`;
  a DB trigger enforces **only the assignee can confirm** (`confirmed_at`) — no task is live without it.
- **consent_queue**: you see/act on **only your own** pending items (`requesting_member_id = you`).
- **agent_runs / change_log**: members read (don't surface loudly — audit/detail views).
- **jobs**: no access (backend queue).

## 5. The screens the data model implies

1. **Group room (chat)** — `messages` where `thread_type='group'`, Realtime-subscribed.
   Special renders: AI messages; **memory diff cards** (`memory_compilations.diff_message_id` points
   at an AI message like "Memory updated — 2 added, 1 revised, 0 removed." → render as a card with
   *view changes* → list that compilation's `memory_versions` → one-tap revert = insert `memory_reverts`);
   deletion placeholders; per governance ruling, proactive AI observations get a one-tap
   "remove + don't do this again" affordance.
2. **Private AI thread** — `messages` where `thread_type='private' and thread_owner_id = me`.
   Nudges arrive here. Talking to the AI = `POST /agent/turn`, then render `reply` (persisting AI
   replies to messages is a pending backend piece — for now the frontend can insert the user's
   message itself and show the reply from the HTTP response).
3. **Team wiki** — the memory surface. Query `memory_pages` + active `memory_versions`
   (join `memory_entries` on `page_id`, filter `is_active`), group facts under page titles, show
   citations (`memory_citations` → link to source message/document) and revision history
   (inactive versions, `valid_from/valid_until`). Entries with `page_id null` = "Uncategorized".
   (A Python renderer exists at `pipeline/wiki.py::render_team_wiki` as the reference layout.)
4. **Tasks board** — statuses above + the confirm affordance for the assignee. `milestones` alongside.
5. **Consent inbox** — your pending `consent_queue` items: show **literal tool name + literal args +
   source snippet** (that's a hard design rule). Approve = update `status='pending'→'approved'`
   ⚠️ **but execution is a backend step** (`execute_consent`) with **no HTTP endpoint yet** — see gaps.
6. **Documents** — upload to Storage + insert `documents` row; open tracking via `document_opens`.
   ⚠️ upload→pipeline glue missing (see gaps).
7. **Contribution** — `contribution_v` view (bars only; no rankings by design).

**The group-room visual layout is an explicitly OPEN design question** (chat vs board vs wiki
arrangement) — owner never settled it; you have latitude, confirm big choices with them.

## 6. Known gaps you will hit (planned backend work, don't work around silently)

*(The original 1–4 were closed on 2026-07-19 — see §3. What remains:)*

0. **Invites only reach existing profiles** — RLS hides `profiles` you don't share a team with, so
   inviting a brand-new user by email needs a backend endpoint. The UI surfaces this rather than
   failing silently.
0b. ~~**No tier / second-key columns on `consent_queue`**~~ — RESOLVED, then partly reversed. The
   `tier` column shipped 2026-07-19 and the observation-suppression affordance is built. The T3
   two-key card was removed entirely on 2026-08-12 (findings §10): see §7 below.
0c. **`document_opens` is per-viewer by RLS** — "opened by 3 of 4" is impossible client-side; the UI
   shows "opened by you / not opened yet". Needs an aggregate view if the fuller signal is wanted.

1. **Chat→memory trigger unwired** — `enqueue_chat_compile` exists; nothing calls it yet (event-bus slice).
   Until then the wiki only grows from document ingestion.
2. **No cron/webhooks/event bus**; GitHub ingestion tables exist but nothing writes them, so the
   contribution screen's GitHub half has no data.
3. **Document ingest double-uploads** — the client sends bytes to Storage *and* to `/documents/{id}/ingest`,
   because v1 carries content inline in the job payload. Fetching by `storage_path` server-side is a
   later slice that removes the second upload.
4. **Agent turns are synchronous** — `/agent/turn` blocks for the whole turn. Fine at pilot scale; a
   streaming or job-backed turn is a later slice.
5. Consent TTL is 7 days in code (schema comment says ~5 min — code wins).
6. `user_session()` still connects via the postgres superuser URL and `SET ROLE`s down. Correct
   behaviour, wrong principal — production needs a dedicated least-privilege authenticator role.
7. **Rate limiting covers `/agent/turn` only** — team-scoped and turn-count-based (not tokens).
   Other endpoints are unlimited; they are cheap RLS'd DB writes.
8b. **The agent reads the wiki but never writes it** — the page index (titles +
   descriptions) is auto-loaded into its instruction each turn, Claude-Code
   style, and `memory_read_page(title)` pulls one page with its citations on
   demand. Nothing sends the whole wiki into a turn. The compiler remains the
   sole writer; `Role.AGENT` has `select` only on `memory_*`.
8. **Storage RLS is tenant-scoped by path prefix** — uploads MUST keep the
   `{team_id}/{uuid}-{filename}` shape or the policy rejects them. Bucket `documents` is private;
   read via `createSignedUrl`.

## 7. Governance rulings that shape UX (owner-accepted, provisional)

Tiers by blast radius, not rank: **T0** read-only → runs instantly; **T1** affects one member →
*that member* consents (assignee-confirm generalized); **T2** shared + reversible → act + visible
card + one-tap revert (memory writes live here). Anyone can start tasks; anyone can stop/revert;
the AI never arbitrates between peers. An "Agent Inbox" (batched approvals) is the intended
long-term consent UX.

**T3 and the two-key countersign were REMOVED 2026-08-12** (findings §10, executed 2026-08-29).
The consent queue now holds exactly one shape: needs the requester's key. `tier` survives as an
informational label, narrowed to T0–T2, seeding the earned-trust ratchet.

The removal's reason is narrower than "two-key was overhead": for **code**, GitHub branch
protection is a stronger second key than the trigger ever was, enforced by the system that owns
the resource (findings §16.2). **For non-code actions nothing replaces it** — after this change
there is no forced second pair of eyes on a non-code action, and nothing currently plans one
(findings §24.1). Recorded deliberately rather than left to be rediscovered.

Full detail lives in the assistant's memory directory, not in this repo. Two
platform-findings documents were cited by name here for months and never
existed as files; naming them again — even to say they are missing — only
makes the next reader look for them.

## 8. Running the stack locally

```bash
# prereqs: Docker Desktop running, uv, node (for supabase CLI via npx)
npx supabase start                  # local stack; prints API URL + anon key → frontend .env
npx supabase migration up           # apply migrations (or: npx supabase db reset)
psql "<admin-url>" -f scripts/setup_local_roles.sql   # worker LOGIN roles (after a db reset)
cp .env.example .env                # backend env (DB role URLs, GEMINI_API_KEY)
uv run uvicorn server.app:app --reload   # agent API on :8000
uv run pytest -q                    # 98 tests; live ones need GEMINI_API_KEY + DB
PYTHONPATH=. uv run python scripts/smoke_pipeline.py  # see the memory compiler work end-to-end
```
Local DB: `127.0.0.1:54322`; Studio: `127.0.0.1:54323`. Tests seed/clean their own data
(`tests/_seed.py` — also a good reference for realistic rows).

## 9. Reading list (in order)

1. `README.md` — repo layout.
2. `supabase/migrations/20260612094142_init.sql` — the whole data model, well-commented.
3. `...095500_rls.sql` + `...120000_action_consent.sql` + `...110000_memory_pages.sql` — permissions ground truth.
4. `AGENTS.md` — the five rules this codebase learned the hard way, and how to
   run its gates. Read before changing anything.
5. `docs/agent-architecture-findings-2026-08-12.md` — the audit the agent's
   shape came out of. (Two findings docs were cited here for months and never
   existed in the repo; that history is in the assistant's memory instead.)
6. `pipeline/wiki.py`, `pipeline/chat.py`, `shared/consent.py` — reference semantics for wiki view, chat capture, consent flow.
7. `docs/superpowers/plans/` — how prior slices were specced/executed.

## 10. Conventions

Conventional commits (`feat:`/`fix:`/`docs:`…), no attribution trailers. Branch per slice
(`feat/<slice>`), merge to master when green. Python side: `uv` only, tests must stay green
(`uv run pytest -q`). Don't modify `supabase/migrations/*` retroactively — always add new ones.
Owner's working style: propose direction at decision points, then execute the approved slice
end-to-end; present a manifest before anything destructive.
