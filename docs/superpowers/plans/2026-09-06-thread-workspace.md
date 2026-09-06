# Thread Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver one-click public threads and inspectable streamed agent activity without weakening thread privacy.

**Architecture:** Reuse canonical threads, the existing NDJSON stream, and durable `agent_steps`. A small read API gates historical activity through the existing thread-access check. PostgreSQL derives the first title while atomically queueing the first agent turn.

**Tech Stack:** React, TypeScript, FastAPI, PostgreSQL/Supabase, Vitest, pytest.

**Spec:** `docs/superpowers/specs/2026-09-06-thread-workspace-design.md`

## Global Constraints

- Team-visible means `visibility='team'`; restricted-thread RLS must remain unchanged.
- Do not add dependencies or a second streaming protocol.
- Render only redacted activity data.

---

### Task 1: Correct clone status

**Files:**
- Modify: `server/github_connect.py`
- Test: `tests/test_github_connect.py`

- [ ] Add a failing test for a failed sync older than `github_repos.last_cloned_at`.
- [ ] Query only failures newer than the successful clone.
- [ ] Run `uv run pytest tests/test_github_connect.py -q`.

### Task 2: Create and title public threads

**Files:**
- Create: `supabase/migrations/20260906100000_thread_first_message_title.sql`
- Modify: `frontend/src/screens/Threads.tsx`, `frontend/src/components/Sidebar.tsx`
- Test: `tests/test_threads.py`, `frontend/tests/component/Threads.test.tsx`

- [ ] Add failing database and component tests for the atomic first title and one-click thread creation.
- [ ] Add the migration and replace the creation form with direct creation.
- [ ] Load allowed threads in the sidebar and navigate to the new row.
- [ ] Run focused pytest and Vitest files.

### Task 3: Inspect agent activity

**Files:**
- Modify: `shared/agent_runs.py`, `server/app.py`, `frontend/src/lib/agentApi.ts`, `frontend/src/screens/GroupRoom.tsx`
- Create: `frontend/src/components/AgentActivity.tsx`
- Test: `tests/test_agent_runs.py`, `frontend/tests/component/GroupRoom.test.tsx`

- [ ] Add failing tests for an authorized historical run response and expandable live activity output.
- [ ] Add the thread-authorized run list API, stream frame fields, and redacted collapsible activity cards.
- [ ] Run focused pytest and Vitest files, then build the frontend.

### Task 4: Verify integration

**Files:** no production files expected

- [ ] Run backend tests covering modified modules, frontend component tests, frontend typecheck, and production build.
- [ ] Inspect the final diff for unrelated changes and report any unverified deployment follow-up.
