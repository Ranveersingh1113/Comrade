"""A turn that outgrows its estimate has to be stopped while it is running.

🔴 THE DEFECT. Admission reserved 6,000 tokens — a measured TRIVIAL turn — and
then let the run make up to `agent_max_llm_calls` (20) model calls with nothing
between them checking what they cost. A repository sweep that reads file after
file carries the whole growing context into every call, so one admitted turn
could spend several million tokens against a 500,000-per-hour cap. The cap was
not wrong about the number; it simply found out afterwards. `finalize_usage`
reconciled the truth when the turn was over, which is accounting, not a brake.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import psycopg

from shared.config import settings
from shared.usage import claim_budget, claimed_tokens
from tests._seed import A1, TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _bucket(tokens: int) -> None:
    conn = _admin()
    try:
        conn.execute(
            "insert into public.usage_buckets (team_id, bucket, turns, tokens)"
            " values (%s, date_trunc('hour', now()), 1, %s)"
            " on conflict (team_id, bucket) do update"
            "   set turns = 1, tokens = excluded.tokens",
            (TEAM_A, tokens),
        )
    finally:
        conn.close()


def _run(reserved: int, status: str = "running") -> str:
    conn = _admin()
    try:
        thread_id = conn.execute(
            "insert into public.threads (team_id, title, visibility, kind,"
            " created_by) values (%s,'budget','team','discussion',%s)"
            " returning id", (TEAM_A, A1),
        ).fetchone()[0]
        run_id = conn.execute(
            "insert into public.agent_runs (team_id, thread_id, requester_id,"
            " status, trigger_type, tokens_reserved)"
            " values (%s,%s,%s,%s,'user',%s) returning id",
            (TEAM_A, thread_id, A1, status, reserved),
        ).fetchone()[0]
    finally:
        conn.close()
    return str(run_id)


# ---------------------------------------------------------------------------

def test_a_run_starts_with_what_admission_claimed_for_it(seeded):
    """`tokens_reserved` is what this run has CLAIMED, and the admission
    estimate is simply its first claim."""
    _bucket(settings.agent_tokens_estimate)
    run_id = _run(settings.agent_tokens_estimate)

    assert claimed_tokens(TEAM_A, run_id) == settings.agent_tokens_estimate


def test_a_run_on_an_exhausted_hour_cannot_claim_more(seeded):
    """Everything else in the hour is already committed to other work.

    🔴 This used to read the headroom and hand the SAME number to every
    concurrent run, so two turns were each told they could spend nearly the
    whole cap. Claiming is the fix: there is nothing left to take.
    """
    _bucket(settings.agent_tokens_per_hour)
    run_id = _run(settings.agent_tokens_estimate)

    assert claim_budget(TEAM_A, run_id) is False


def test_an_uncapped_team_has_no_ceiling(seeded, monkeypatch):
    monkeypatch.setattr(settings, "agent_tokens_per_hour", 0)
    monkeypatch.setattr(settings, "agent_turns_per_hour", 0)
    run_id = _run(settings.agent_tokens_estimate)

    assert claimed_tokens(TEAM_A, run_id) is None
    assert claim_budget(TEAM_A, run_id) is True


def test_a_run_that_claimed_nothing_can_still_claim(seeded):
    """`record_reservation` is written after the run exists and can be lost.
    That must overcharge, never refuse the run its first model call."""
    _bucket(0)
    run_id = _run(0)

    assert claimed_tokens(TEAM_A, run_id) == 0
    assert claim_budget(TEAM_A, run_id) is True


# ---------------------------------------------------------------------------
# The brake itself, through the real turn loop
# ---------------------------------------------------------------------------

def _expensive_model(monkeypatch, per_call: int, calls: int = 20):
    """A runner whose every call reports `per_call` tokens and asks for more.

    Shaped like the run this exists for: a sweep that reads file after file,
    each call carrying the whole grown context, none of them finishing.
    """
    made: list[int] = []

    def _event(n: int):
        part = MagicMock()
        part.function_call = None
        part.function_response = None
        part.text = f"read file {n}"
        event = MagicMock()
        event.content.parts = [part]
        event.usage_metadata = SimpleNamespace(
            prompt_token_count=per_call,
            candidates_token_count=0,
            total_token_count=per_call,
        )
        return event

    async def _fake_run(*_a, **_k):
        for n in range(calls):
            made.append(1)
            yield _event(n)

    monkeypatch.setattr("agent.runtime.Runner.run_async", _fake_run)
    return made


def _thread_id() -> str:
    conn = _admin()
    try:
        row = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM_A,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def _drain(thread_id: str) -> list[dict]:
    from agent.runtime import stream_turn

    async def _go():
        return [
            f async for f in stream_turn(
                TEAM_A, A1, "sweep the repository", thread_id=thread_id
            )
        ]

    return asyncio.run(_go())


def test_a_turn_is_stopped_the_moment_it_passes_the_hourly_budget(
    seeded, monkeypatch,
):
    """🔴 Nothing checked between model calls. A 20-call turn could spend the
    hour several times over, and `finalize_usage` reconciled it afterwards —
    accounting, not a brake."""
    _bucket(0)
    # A quarter of the hour's whole budget per call. The fourth is the one
    # that cannot be afforded.
    made = _expensive_model(monkeypatch, settings.agent_tokens_per_hour // 4)

    frames = _drain(_thread_id())

    assert len(made) < 20, "the turn ran every call it was allowed"
    assert frames[-1]["type"] == "over_budget", [f["type"] for f in frames]


def test_what_it_managed_before_stopping_is_kept(seeded, monkeypatch):
    """The check runs AFTER the event's steps are recorded, not before. The
    call that triggered it cannot be taken back and has already been paid for,
    so discarding what it produced would cost the team the money AND the work.
    """
    _bucket(0)
    made = _expensive_model(monkeypatch, settings.agent_tokens_per_hour // 4)

    frames = _drain(_thread_id())

    # Every call this turn made produces exactly one text step, so equality is
    # the assertion with teeth: checking BEFORE the steps land drops the last
    # call's work and leaves one fewer than were paid for. Earlier calls'
    # steps survive either way, which is why a bare "some steps exist" would
    # pass on the broken order too.
    run_id = frames[0]["run_id"]
    conn = _admin()
    try:
        steps = conn.execute(
            "select count(*) from public.agent_steps where run_id=%s", (run_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert len(made) > 1, "the turn has to make several calls for this to mean anything"
    assert steps == len(made), (
        f"{len(made)} calls were paid for and only {steps} steps were kept"
    )


def test_the_member_is_told_why_it_stopped(seeded, monkeypatch):
    """A turn that vanishes mid-answer is indistinguishable from a hang."""
    _bucket(0)
    _expensive_model(monkeypatch, settings.agent_tokens_per_hour // 4)

    detail = _drain(_thread_id())[-1]["detail"]

    assert "budget" in detail.lower()


def test_an_over_budget_run_is_closed_rather_than_left_running(
    seeded, monkeypatch,
):
    _bucket(0)
    _expensive_model(monkeypatch, settings.agent_tokens_per_hour // 4)

    run_id = _drain(_thread_id())[0]["run_id"]

    conn = _admin()
    try:
        status, finished = conn.execute(
            "select status, finished_at is not null from public.agent_runs"
            " where id=%s", (run_id,),
        ).fetchone()
    finally:
        conn.close()
    assert (status, finished) == ("failed", True)


def test_an_affordable_turn_runs_every_call_it_asked_for(seeded, monkeypatch):
    """The brake must not fire on ordinary work. Twenty calls at 1,000 tokens
    is more than the 6,000 estimate and well inside the hour."""
    _bucket(0)
    made = _expensive_model(monkeypatch, 1_000)

    frames = _drain(_thread_id())

    assert len(made) == 20
    assert frames[-1]["type"] != "over_budget"


def test_what_the_over_budget_turn_really_cost_is_recorded(seeded, monkeypatch):
    """It stopped, but it still spent. Charging only the estimate would hand
    the team back the tokens it just burned."""
    _bucket(0)
    per_call = settings.agent_tokens_per_hour // 4
    _expensive_model(monkeypatch, per_call)

    run_id = _drain(_thread_id())[0]["run_id"]

    conn = _admin()
    try:
        used = conn.execute(
            "select input_tokens from public.agent_runs where id=%s", (run_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert used >= per_call, "the calls it did make must be on the record"


# ---------------------------------------------------------------------------
# The batch caller
# ---------------------------------------------------------------------------

def test_run_turn_does_not_raise_when_a_turn_stops_early(seeded, monkeypatch):
    """🔴 `run_turn` ends with `final["run_id"]`, and `final` is `{}` until a
    `final` frame arrives. It enumerated the frames that never send one —
    busy, empty, waiting_for_permission — and KeyError-ed on any other. So
    every new way for a turn to stop early re-opened the same hole, and this
    is the third time it has been dug: the agent worker drives turns through
    `run_turn_sync`, where the exception is a crashed worker rather than a
    handled outcome."""
    from agent.runtime import run_turn

    _bucket(0)
    _expensive_model(monkeypatch, settings.agent_tokens_per_hour // 4)

    result = asyncio.run(run_turn(
        TEAM_A, A1, "sweep the repository", thread_id=_thread_id(),
    ))

    assert result["run_id"] is not None, "the run row exists and is worth returning"
    assert result["reply"] == ""
    assert "over_budget" in result


def test_run_turn_does_not_raise_when_a_run_is_cancelled(seeded, monkeypatch):
    """The same hole, dug by T11 and never closed: a member cancelling a run
    that the agent worker is driving yields `cancelled`, which no branch
    caught."""
    from agent import runtime

    part = MagicMock()
    part.function_call = None
    part.function_response = None
    part.text = "working"
    event = MagicMock()
    event.content.parts = [part]
    event.usage_metadata = None

    async def _fake_run(*_a, **_k):
        yield event
        yield event

    monkeypatch.setattr("agent.runtime.Runner.run_async", _fake_run)
    # Somebody pressed cancel: the run is no longer this worker's.
    monkeypatch.setattr(runtime, "_still_ours", lambda *a, **k: False)

    result = asyncio.run(runtime.run_turn(
        TEAM_A, A1, "keep going", thread_id=_thread_id(),
    ))

    assert result["cancelled"] is True
    assert result["run_id"] is not None


# ---------------------------------------------------------------------------
# A crash between reserving and linking
# ---------------------------------------------------------------------------

def _bucket_tokens() -> int:
    conn = _admin()
    try:
        row = conn.execute(
            "select tokens from public.usage_buckets where team_id=%s"
            " and bucket=date_trunc('hour', now())", (TEAM_A,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


def test_a_run_whose_reservation_was_never_linked_does_not_earn_budget(seeded):
    """`record_reservation` is written AFTER the run exists, so a process
    killed between the two leaves a run with no `tokens_reserved`. Finalizing
    it must charge what it cost on top of the estimate already taken — an
    overcharge of the estimate, which is the safe direction for a cap — and
    never hand the team free budget by refunding a reservation it cannot see.
    """
    from shared.usage import finalize_usage

    _bucket(settings.agent_tokens_estimate)
    # Terminal: settling is something a run does on its way out, and a run
    # that is still `running` is now refused because an obsolete worker used
    # to settle one a replacement was still working on (fix.md F43).
    run_id = _run(0, status="done")   # the linkage that never happened

    finalize_usage(TEAM_A, run_id, 1_000)

    assert _bucket_tokens() == settings.agent_tokens_estimate + 1_000


def test_reconciliation_happens_exactly_once(seeded):
    """The run finishes on the success path, the failure path, the cancel path
    and now the budget path. A second reconciliation would silently hand the
    team back tokens it spent."""
    from shared.usage import finalize_usage

    _bucket(settings.agent_tokens_estimate)
    run_id = _run(settings.agent_tokens_estimate, status="done")

    finalize_usage(TEAM_A, run_id, 1_000)
    after_first = _bucket_tokens()
    finalize_usage(TEAM_A, run_id, 1_000)

    assert _bucket_tokens() == after_first == 1_000
