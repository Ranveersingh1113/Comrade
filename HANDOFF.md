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
  ├─ agent/                ADK LlmAgent (Gemini 2.5 Flash), 4 tools, propose-only
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

| Endpoint | Status | Notes |
|---|---|---|
| `POST /agent/turn` `{team_id, requester_id, text}` → `{run_id, reply}` | ✅ built | ⚠️ **identity comes from the request body — DEV ONLY.** JWT binding is a pending backend slice. Build the frontend to send the logged-in user's ids for now; expect this endpoint to switch to `Authorization: Bearer <supabase JWT>` later. |
| `GET /health` | ✅ built | |
| Everything else | **direct Supabase** | tables + Realtime + Storage via supabase-js under RLS |

Run it: `uv run uvicorn server.app:app --reload` (port 8000).

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

1. **Auth on `/agent/turn`** — body-supplied identity, dev-only (slice: JWT binding).
2. **No consent execute endpoint** — `approve/reject/edit_and_approve → execute_consent` exist as
   Python (`shared/consent.py`) but aren't exposed over HTTP. Frontend can flip status via RLS, but
   nothing executes it yet.
3. **No document-upload→job glue** — `enqueue_document` is backend-only (members can't insert `jobs`).
   Needs a small endpoint or DB trigger.
4. **AI replies not persisted to `messages`** — `/agent/turn` returns the reply; persistence is a
   deferred slice.
5. **Chat→memory trigger unwired** — `enqueue_chat_compile` exists; nothing calls it yet (event-bus slice).
6. **No cron/webhooks/event bus**; GitHub ingestion tables exist but nothing writes them.
7. Consent TTL is 7 days in code (schema comment says ~5 min — code wins).

## 7. Governance rulings that shape UX (owner-accepted, provisional)

Tiers by blast radius, not rank: **T0** read-only → runs instantly; **T1** affects one member →
*that member* consents (assignee-confirm generalized); **T2** shared + reversible → act + visible
card + one-tap revert (memory writes live here); **T3** external/irreversible/money → **two keys:
initiator + any other member** (the person affected must be a key if the action is about them).
Anyone can start tasks; anyone can stop/revert; the AI never arbitrates between peers.
Hard floors: money + outbound-to-non-members never drop below T3. An "Agent Inbox" (batched
approvals) is the intended long-term consent UX. Full detail: memory + `docs/comrade-platform-findings.md`.

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
4. `docs/comrade-platform-findings.md` — platform direction, security model, roadmap.
5. `docs/comrade-memory-ingestion-findings.md` (heed the 2026-07-15 supersession banner) — memory design history.
6. `pipeline/wiki.py`, `pipeline/chat.py`, `shared/consent.py` — reference semantics for wiki view, chat capture, consent flow.
7. `docs/superpowers/plans/` — how prior slices were specced/executed.

## 10. Conventions

Conventional commits (`feat:`/`fix:`/`docs:`…), no attribution trailers. Branch per slice
(`feat/<slice>`), merge to master when green. Python side: `uv` only, tests must stay green
(`uv run pytest -q`). Don't modify `supabase/migrations/*` retroactively — always add new ones.
Owner's working style: propose direction at decision points, then execute the approved slice
end-to-end; present a manifest before anything destructive.
