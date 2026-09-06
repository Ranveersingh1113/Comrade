# Thread Workspace Design

## Goal

Make a team’s conversations feel like a modern agent workspace: immediate public thread creation, a navigable thread sidebar, and inspectable agent work.

## Decisions

- A new thread is created immediately as a team-visible discussion called `New thread`. The existing `threads` table and RLS remain the authority; restricted threads remain invisible to people outside them.
- The first agent-directed message renames only a `New thread`, atomically with queueing the turn. The title is a normalized, bounded copy of that message, not a model-generated second request.
- The existing durable `agent_steps` log is the source for live and historical activity. The API authorizes a member against the requested thread before returning its runs. The browser never reads this sensitive table directly.
- Activity items are collapsed by default. Details show redacted JSON, command output, and an existing `DiffView` when a tool supplied a patch.
- A clone failure is current only when it is newer than the repository’s last successful clone. A previous failed job must not override a successful checkout.

## Boundaries

- No new streaming transport, thread table, UI dependency, or model call.
- Thread settings are out of scope: creation defaults to team-visible discussion, preserving privacy rules without adding a new settings surface in this pass.
- Values named token, secret, password, authorization, cookie, or key are redacted before rendering activity details.

## Verification

- Database test proves first agent turn retitles exactly once.
- API test proves historical runs are returned only for an accessible thread.
- Component tests prove one-click creation, sidebar visibility, expandable activity details, and retained clone success status.
