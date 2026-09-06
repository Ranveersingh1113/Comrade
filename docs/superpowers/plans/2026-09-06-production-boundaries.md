# Production boundaries: first implementation batch

Superseded by [the master plan](2026-09-06-production-readiness-master-plan.md).
Retained as the initial scope record; implementation is paused for master planning.

Goal: close immediate preview isolation failures, make the packaged execution
stack usable, and keep active conversations readable beyond 500 messages.

Scope follows the September 6 source audit and the user's authorization to choose
implementation order. This is the first batch, not completion of the entire
multiplayer roadmap. No production deployment, reset, or publication is included.

## Order and acceptance

1. Preview browser boundary (`server/previews.py`, `server/app.py`): enforce a
   response CSP sandbox without same-origin privileges, remove upstream cookie
   and encoding headers, and bound bytes while reading responses. A real browser
   must execute preview JavaScript but refuse access to Comrade local storage.
   This native boundary intentionally excludes preview storage/service workers;
   a future dedicated per-preview origin may restore those capabilities.
2. Preview network/process boundary (`agent/processes.py`): one internal network
   per process, only the labeled API container joins it; no-port processes have
   no network. Persist the container name before launch. Cleanup failures must
   remain retryable rather than recording a false stop. Regression tests cover
   distinct networks, launch interruption, and failed removal.
3. Deployment: install Docker CLI, configure socket GID for both execution
   workers, migrate before replacing services, serialize releases, and run the
   actual frontend build in release gates. Exercise deploy shell behavior with
   controlled command executables; never deploy during tests.
4. Conversation history: newest bounded page plus older-page navigation, stable
   ordering and regression coverage with more than 500 messages.
5. Review combined changes and run focused checks followed by release checks
   where available. Record environmental blockers explicitly.

Deferred: full run reconnect/cancellation UX, memory provenance/scopes, CI
feedback, attachment scopes, quality evaluations, and full operational recovery.
These remain audit findings, not claims addressed by this batch.

## Method

Write regression tests, observe the intended failures, apply the smallest fix,
and rerun. Reuse existing pytest/Vitest/Playwright and Docker tools. No new runtime
libraries. Work on `codex/production-boundaries`; preserve existing local files
and credentials. Deployment implementation is delegated independently from
preview code; review all shared configuration before completion.
