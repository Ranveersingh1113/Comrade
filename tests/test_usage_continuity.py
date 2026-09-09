"""What a turn is charged when it stops, waits, or changes hands.

Three defects from the 2026-09-09 review, all in the accounting F30 left
behind. F30 made the CLAIM atomic, which was the right fix for two runs
dividing one budget by reading it. It did not make the run's own record of
what it spent survive the things a run actually does.

🔴 F42 — A PERMISSION WAIT RESET THE TOTAL. `pause_for_permission` parked the
run without recording what it had spent, and the generator's counters start at
zero on resume. `finish_run` writes the token columns ABSOLUTELY, so a turn
that waited twice was recorded as having cost only its last segment — while
`tokens_reserved`, the claimed allocation, stayed cumulative. The two
disagreed: in the team's favour on the cap, against them on the record.

🔴 F43 — A STOP DURING THE FIRST CALL CHARGED NOTHING. The ownership check ran
BEFORE the arriving event's usage was read, so a cancellation landing while the
first model response was in flight finalized zero tokens and refunded the whole
estimate. The provider had already been paid. The same path runs on lease loss,
where it also let an obsolete worker settle a run a replacement was still
working on.

🔴 F44 — THE HOUR BOUNDARY REFUSED A RUN THAT HAD BUDGET. `claim_budget`
updated the bucket for `date_trunc('hour', now())`, which across a boundary may
not exist yet — buckets are created at admission. A run admitted at 10:59 was
refused at 11:00 with a full unused hour in front of it, unless somebody else's
request happened to create the row first. Behaviour depended on unrelated
traffic. `finalize_usage` had the mirror image and skipped reconciliation
entirely for a run that started in an earlier hour, so its unspent estimate was
never returned.
"""
import datetime

import psycopg
import pytest

from shared.agent_runs import finish_run, pause_for_permission, usage_so_far
from shared.config import settings
from shared.usage import claim_budget, claimed_tokens, finalize_usage
from tests._seed import A1, TEAM_A

CHUNK = settings.agent_tokens_estimate


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _thread(conn) -> str:
    return str(conn.execute(
        "select id from public.threads where team_id=%s and title='General'",
        (TEAM_A,),
    ).fetchone()[0])


@pytest.fixture
def run(seeded):
    """One admitted run, and this hour's bucket, as admission leaves them."""
    conn = _admin()
    try:
        conn.execute("delete from public.usage_buckets where team_id=%s", (TEAM_A,))
        conn.execute("delete from public.agent_runs where team_id=%s", (TEAM_A,))
        thread_id = _thread(conn)
        run_id = str(conn.execute(
            "insert into public.agent_runs (team_id, requester_id, thread_id,"
            " trigger_type, input_summary, status, tokens_reserved)"
            " values (%s,%s,%s,'user','probe','running',%s) returning id",
            (TEAM_A, A1, thread_id, CHUNK),
        ).fetchone()[0])
        conn.execute(
            "insert into public.usage_buckets (team_id, bucket, turns, tokens)"
            " values (%s, date_trunc('hour', now()), 1, %s)",
            (TEAM_A, CHUNK),
        )
        yield run_id
    finally:
        conn.close()


def _tokens(bucket_sql: str = "date_trunc('hour', now())") -> int:
    conn = _admin()
    try:
        row = conn.execute(
            f"select tokens from public.usage_buckets where team_id=%s"
            f" and bucket={bucket_sql}", (TEAM_A,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


def _recorded(run_id: str) -> tuple[int, int]:
    conn = _admin()
    try:
        return tuple(conn.execute(
            "select coalesce(input_tokens,0), coalesce(output_tokens,0)"
            " from public.agent_runs where id=%s", (run_id,),
        ).fetchone())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# F42 — the total survives a permission wait
# ---------------------------------------------------------------------------

def test_parking_a_run_records_what_it_has_spent(run):
    """🔴 The checkpoint that did not exist. Without it a resume starts from
    zero and the earlier segments are gone."""
    pause_for_permission(TEAM_A, run, None, 700, 300)

    assert _recorded(run) == (700, 300)


def test_a_resumed_run_continues_from_the_recorded_total(run):
    """What the generator reads when it picks the run back up."""
    pause_for_permission(TEAM_A, run, None, 700, 300)

    assert usage_so_far(TEAM_A, run) == (700, 300)


def test_two_permission_cycles_charge_all_three_segments(run):
    """The finding's own acceptance. Segment 1 waits, segment 2 waits,
    segment 3 finishes; the run must be recorded as costing all three."""
    conn = _admin()
    try:
        pause_for_permission(TEAM_A, run, None, 700, 300)         # 1000
        conn.execute("update public.agent_runs set status='running'"
                     " where id=%s", (run,))
        carried_in, carried_out = usage_so_far(TEAM_A, run)
        pause_for_permission(TEAM_A, run, None,
                             carried_in + 500, carried_out + 200)  # 1700
        conn.execute("update public.agent_runs set status='running'"
                     " where id=%s", (run,))
        carried_in, carried_out = usage_so_far(TEAM_A, run)
        finish_run(TEAM_A, run, "done",
                   input_tokens=carried_in + 100,
                   output_tokens=carried_out + 50)                 # 1850
    finally:
        conn.close()

    assert _recorded(run) == (1300, 550)


def test_the_recorded_total_is_what_settles_against_the_bucket(run):
    """Accounting that only counts the last segment refunds the difference
    between the estimate and a total that never happened."""
    pause_for_permission(TEAM_A, run, None, 700, 300)
    conn = _admin()
    try:
        conn.execute("update public.agent_runs set status='running'"
                     " where id=%s", (run,))
    finally:
        conn.close()
    used_in, used_out = usage_so_far(TEAM_A, run)
    finish_run(TEAM_A, run, "done", input_tokens=used_in, output_tokens=used_out)
    finalize_usage(TEAM_A, run, used_in + used_out)

    # The bucket held the estimate; it now holds what was actually spent.
    assert _tokens() == 1000


# ---------------------------------------------------------------------------
# F43 — a stop does not make the tokens free
# ---------------------------------------------------------------------------

def test_a_cancelled_run_is_still_charged_for_what_arrived(run):
    """🔴 The event that was already generated and already paid for. The
    ownership check used to run first and return, so this cost nothing."""
    finish_run(TEAM_A, run, "cancelled", input_tokens=800, output_tokens=400)
    finalize_usage(TEAM_A, run, 1200)

    assert _recorded(run) == (800, 400)
    assert _tokens() == 1200, "a response the provider billed for was refunded"


def test_a_stale_worker_cannot_settle_a_run_someone_else_is_running(run):
    """Lease takeover. The old worker walks its exit path while a replacement
    is still working; settling there marks the run finalized and the real
    owner's settlement is skipped, leaving the estimate on the bucket."""
    conn = _admin()
    try:
        conn.execute(
            "update public.agent_runs set worker_id='replacement',"
            " status='running' where id=%s", (run,))
    finally:
        conn.close()

    finalize_usage(TEAM_A, run, 50)          # the obsolete worker

    conn = _admin()
    try:
        finalized = conn.execute(
            "select usage_finalized_at from public.agent_runs where id=%s",
            (run,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert finalized is None, "an obsolete worker settled a live run"
    assert _tokens() == CHUNK, "the replacement's estimate was refunded"


def test_the_real_owner_can_still_settle_when_it_finishes(run):
    """And the fence must not lock the run out of ever settling."""
    conn = _admin()
    try:
        conn.execute("update public.agent_runs set worker_id='replacement'"
                     " where id=%s", (run,))
    finally:
        conn.close()
    # As the owner, which is what the fence now requires: a caller with no
    # identity may settle only a run nobody executed.
    finalize_usage(TEAM_A, run, 50, worker_id="replacement")   # refused: live
    finish_run(TEAM_A, run, "done", input_tokens=40, output_tokens=10,
               worker_id="replacement")
    finalize_usage(TEAM_A, run, 50, worker_id="replacement")

    assert _tokens() == 50


# ---------------------------------------------------------------------------
# F44 — the hour boundary
# ---------------------------------------------------------------------------

def _move_run_to_last_hour(run_id: str) -> None:
    """As if this run had been admitted just before the hour turned."""
    conn = _admin()
    try:
        conn.execute(
            "update public.agent_runs set created_at = now() - interval '1 hour',"
            " usage_bucket = date_trunc('hour', now() - interval '1 hour')"
            " where id=%s", (run_id,))
        conn.execute(
            "update public.usage_buckets"
            "   set bucket = date_trunc('hour', now() - interval '1 hour')"
            " where team_id=%s", (TEAM_A,))
    finally:
        conn.close()


def test_a_run_that_crosses_the_hour_can_still_claim(run):
    """🔴 The finding's own case. The current hour has no bucket row, and the
    old code read that as "no room" — refusing a run with a full unused
    allowance, unless somebody else's traffic had created the row first."""
    _move_run_to_last_hour(run)

    assert claim_budget(TEAM_A, run, 100) is True


def test_it_claims_the_same_amount_with_or_without_other_traffic(run):
    """"Behavior depends on unrelated traffic" is the defect, so the test is
    that it does not."""
    _move_run_to_last_hour(run)
    alone = claim_budget(TEAM_A, run, 100)

    conn = _admin()
    try:
        conn.execute(
            "insert into public.usage_buckets (team_id, bucket, turns, tokens)"
            " values (%s, date_trunc('hour', now()), 1, 0)"
            " on conflict (team_id, bucket) do nothing", (TEAM_A,))
    finally:
        conn.close()
    with_traffic = claim_budget(TEAM_A, run, 100)

    assert alone == with_traffic is True


def test_the_claim_is_charged_to_the_hour_the_run_was_admitted_in(run):
    """A run's allowance belongs to one hour for its whole life, so a boundary
    can neither refuse it nor let it spend a second hour's cap as well."""
    _move_run_to_last_hour(run)
    claim_budget(TEAM_A, run, 100)

    assert _tokens("date_trunc('hour', now() - interval '1 hour')") == CHUNK + 100
    assert _tokens() == 0, "the new hour was charged for an old run's claim"


def test_settlement_returns_the_estimate_to_the_hour_it_came_from(run):
    """The mirror image: reconciliation used to be skipped entirely for a run
    that began in an earlier hour, so the unspent estimate was never returned
    and the team silently lost budget it had not used."""
    _move_run_to_last_hour(run)
    finish_run(TEAM_A, run, "done", input_tokens=40, output_tokens=10)
    finalize_usage(TEAM_A, run, 50)

    assert _tokens("date_trunc('hour', now() - interval '1 hour')") == 50


def test_the_cap_still_refuses_a_claim_that_would_pass_it(run):
    """The bound the whole mechanism exists for, across the boundary too."""
    _move_run_to_last_hour(run)
    _, token_cap = settings.agent_tokens_per_hour, settings.agent_tokens_per_hour

    assert claim_budget(TEAM_A, run, token_cap) is False
    assert claimed_tokens(TEAM_A, run) == CHUNK

# ---------------------------------------------------------------------------
# F43 — the ordering itself, through the real turn loop
# ---------------------------------------------------------------------------

def test_a_stop_during_the_first_call_still_charges_that_call(seeded, monkeypatch):
    """🔴 The ordering, not just the arithmetic.

    The tests above prove that a cancelled run WITH usage settles correctly.
    This one proves the runtime actually collects it: the ownership check used
    to run before `_usage_from_event`, so a stop landing while the very first
    response was in flight returned with both counters at zero and refunded the
    whole estimate for a call the provider had already billed.

    The cancellation is made real the way one is: the run stops being ours
    between events.
    """
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from agent import runtime

    per_call = 4_000

    def _event(n: int):
        part = MagicMock()
        part.function_call = None
        part.function_response = None
        part.text = f"thinking {n}"
        event = MagicMock()
        event.content.parts = [part]
        event.usage_metadata = SimpleNamespace(
            prompt_token_count=per_call,
            candidates_token_count=0,
            total_token_count=per_call,
        )
        return event

    async def _fake_run(*_a, **_k):
        for n in range(5):
            yield _event(n)

    monkeypatch.setattr("agent.runtime.Runner.run_async", _fake_run)
    # Somebody presses STOP: the run is no longer this worker's, from the very
    # first check — which happens after the first response has arrived.
    monkeypatch.setattr(runtime, "_still_ours", lambda *a, **k: False)

    conn = _admin()
    try:
        conn.execute("delete from public.agent_runs where team_id=%s", (TEAM_A,))
        thread_id = _thread(conn)
    finally:
        conn.close()

    async def _go():
        return [f async for f in runtime.stream_turn(
            TEAM_A, A1, "look into this", thread_id=thread_id)]

    frames = asyncio.run(_go())

    assert any(f.get("type") == "cancelled" for f in frames), frames
    run_id = next(f["run_id"] for f in frames if f.get("type") == "run")
    recorded = _recorded(run_id)
    assert recorded != (0, 0), (
        "a cancelled turn recorded zero tokens for a response that had already"
        " arrived and had already been paid for"
    )
    assert recorded[0] == per_call

def test_a_resumed_turn_adds_to_what_the_earlier_segments_spent(
    run, monkeypatch,
):
    """🔴 The seeding half of F42, through the real turn loop.

    A run parked on a permission card carries its earlier segments in the row.
    When the same run is driven again, the generator must START from those —
    `finish_run` writes the token columns absolutely, so counting from zero
    overwrites everything before the resume with the last segment alone.
    """
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    # Segment one, already recorded and parked.
    pause_for_permission(TEAM_A, run, None, 1_000, 500)
    conn = _admin()
    try:
        conn.execute("update public.agent_runs set status='running'"
                     " where id=%s", (run,))
        thread_id = _thread(conn)
    finally:
        conn.close()

    def _event():
        part = MagicMock()
        part.function_call = None
        part.function_response = None
        part.text = "carrying on"
        event = MagicMock()
        event.content.parts = [part]
        event.usage_metadata = SimpleNamespace(
            prompt_token_count=4_000, candidates_token_count=250,
            total_token_count=4_250,
        )
        return event

    async def _fake_run(*_a, **_k):
        yield _event()

    monkeypatch.setattr("agent.runtime.Runner.run_async", _fake_run)

    from agent import runtime

    async def _go():
        return [f async for f in runtime.stream_turn(
            TEAM_A, A1, "carry on", thread_id=thread_id, run_id=run)]

    asyncio.run(_go())

    assert _recorded(run) == (5_000, 750), (
        "the resumed segment replaced the earlier ones instead of adding to them"
    )

# ---------------------------------------------------------------------------
# F43 reopened — settlement belongs to whoever finished the run
# ---------------------------------------------------------------------------

def test_a_stale_worker_cannot_settle_after_the_replacement_finishes(run):
    """🔴 THE DEFECT (fix.md F43, reopened). My terminal-status fence blocks a
    stale worker while the replacement is RUNNING, and stops the moment it
    finishes.

    The interleaving: a replacement takes the lease, completes the run and
    commits 1,200 tokens. The stale worker then walks its own exit path — its
    `finish_run` is refused on the worker fence, `_finish` catches that
    LookupError, and it settles its LOCAL total of 50 anyway. The run is
    terminal by then, so my fence lets it through. The replacement's own
    settlement is skipped because `usage_finalized_at` is now set, and the
    bucket permanently records 50 for a turn that cost 1,200 — releasing
    budget the team actually spent.

    Terminal status alone cannot say whose totals are authoritative.
    """
    conn = _admin()
    try:
        # The replacement holds the lease and finishes the work.
        conn.execute("update public.agent_runs set worker_id='replacement'"
                     " where id=%s", (run,))
    finally:
        conn.close()
    finish_run(TEAM_A, run, "done", input_tokens=1_000, output_tokens=200,
               worker_id="replacement")

    # The stale worker's exit path, with its own much smaller local totals.
    finalize_usage(TEAM_A, run, 50, worker_id="stale")

    assert _tokens() == CHUNK, (
        "a stale worker settled a run it did not finish, releasing budget the"
        " replacement actually spent"
    )

    # And the real owner can still settle, with the truth.
    finalize_usage(TEAM_A, run, 1_200, worker_id="replacement")
    assert _tokens() == 1_200


def test_the_worker_that_finished_the_run_settles_it(run):
    """The ordinary success path has to keep working: the worker that owns the
    run is the one whose numbers count."""
    conn = _admin()
    try:
        conn.execute("update public.agent_runs set worker_id='mine'"
                     " where id=%s", (run,))
    finally:
        conn.close()
    finish_run(TEAM_A, run, "done", input_tokens=900, output_tokens=100,
               worker_id="mine")

    finalize_usage(TEAM_A, run, 1_000, worker_id="mine")

    assert _tokens() == 1_000


def test_a_cancelled_run_is_still_settled_by_the_worker_that_ran_it(run):
    """The case the LookupError branch exists for, and it must survive the
    fence. `cancel_run` nulls `worker_id`, so the worker that was executing has
    no id to match — it is nonetheless the only thing that knows what the turn
    actually spent, and `usage_owner` is what remembers that.

    🔴 The setup used to jump straight to (cancelled, worker_id=null) with one
    UPDATE, so the run had never been OWNED and the test passed on the old
    rule's blanket allowance rather than on ownership. It now takes the
    ownership first, which is the only way the state it describes arises.
    """
    conn = _admin()
    try:
        conn.execute("update public.agent_runs set worker_id="
                     "'the-worker-that-ran-it', status='running'"
                     " where id=%s", (run,))
        conn.execute(
            "update public.agent_runs set status='cancelled', worker_id=null,"
            " finished_at=now() where id=%s", (run,))
    finally:
        conn.close()

    finalize_usage(TEAM_A, run, 800, worker_id="the-worker-that-ran-it")

    assert _tokens() == 800, (
        "a cancelled turn's real cost was refused, so the estimate stays on"
        " the bucket for the rest of the hour"
    )


def test_settlement_without_a_worker_id_still_works(run):
    """The server cancels a QUEUED run and settles zero with no worker in
    play. That caller has no id to offer and must not be fenced out."""
    conn = _admin()
    try:
        conn.execute("update public.agent_runs set status='cancelled',"
                     " worker_id=null, finished_at=now() where id=%s", (run,))
    finally:
        conn.close()

    finalize_usage(TEAM_A, run, 0)

    assert _tokens() == 0

# ---------------------------------------------------------------------------
# F43 still open — cancellation erased the fence
# ---------------------------------------------------------------------------
#
# THE INVARIANT: a run is settled exactly once, by the identity that actually
# performed the work whose totals are being recorded.
#
# The state transitions that reach (terminal, worker_id IS NULL) — the shape
# the previous fence waved through — are all reached here through the real
# functions rather than by hand:
#
#   * `cancel_run` on an EXECUTING run: the executing worker is the rightful
#     settler, and it is the only thing that knows what the turn spent.
#   * `cancel_run` on a QUEUED run: nobody executed, and the server settles
#     zero with no identity of its own.
#   * `recover_expired_agent_runs` past its attempt limit: 'failed', worker
#     nulled, and the last worker may still be walking its exit path.

def _cancel(run_id: str, requester=A1) -> bool:
    from agent.run_queue import cancel_run

    return cancel_run(TEAM_A, run_id, requester_id=requester)


def _owner_columns(run_id: str) -> tuple:
    conn = _admin()
    try:
        return conn.execute(
            "select worker_id, status from public.agent_runs where id=%s",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()


def test_a_stale_worker_cannot_settle_a_cancelled_replacements_run(run):
    """🔴 THE DEFECT (fix.md F43, still open). The fence accepts ANY caller
    once the stored worker id is NULL, and cancellation is exactly what nulls
    it.

    The sequence, through the real cancel path: the old worker loses its lease,
    a replacement takes over and spends 1,200, a member cancels, and the stale
    worker's exit settles 50 FIRST. It wins `usage_finalized_at`, the
    replacement's real number is skipped, and the bucket keeps 50 for a turn
    that cost 1,200 — releasing budget the team actually spent.
    """
    conn = _admin()
    try:
        conn.execute("update public.agent_runs set worker_id='replacement',"
                     " status='running' where id=%s", (run,))
    finally:
        conn.close()

    assert _cancel(run) is True
    assert _owner_columns(run) == (None, "cancelled")

    finalize_usage(TEAM_A, run, 50, worker_id="stale")

    assert _tokens() == CHUNK, (
        "a worker that lost its lease settled a run the replacement executed"
    )

    finalize_usage(TEAM_A, run, 1_200, worker_id="replacement")
    assert _tokens() == 1_200


def test_the_worker_that_was_executing_settles_a_cancelled_run(run):
    """The case the NULL-owner allowance existed for, and it must survive:
    a member cancels the turn this worker is running, and this worker holds the
    only record of what it cost."""
    conn = _admin()
    try:
        conn.execute("update public.agent_runs set worker_id='mine',"
                     " status='running' where id=%s", (run,))
    finally:
        conn.close()
    assert _cancel(run) is True

    finalize_usage(TEAM_A, run, 800, worker_id="mine")

    assert _tokens() == 800


def test_a_cancelled_queued_run_is_settled_by_the_server(run):
    """Queued-never-executed is a different thing from cancelled-while-running,
    and the server settles it with no identity at all."""
    conn = _admin()
    try:
        conn.execute("update public.agent_runs set worker_id=null,"
                     " status='queued' where id=%s", (run,))
    finally:
        conn.close()
    assert _cancel(run) is True

    finalize_usage(TEAM_A, run, 0)

    assert _tokens() == 0


def test_a_stranger_cannot_settle_a_cancelled_queued_run(run):
    """Nobody executed it, so nobody's totals are authoritative — but a worker
    that was never near this run must not be able to write one either."""
    conn = _admin()
    try:
        conn.execute("update public.agent_runs set worker_id=null,"
                     " status='queued' where id=%s", (run,))
    finally:
        conn.close()
    _cancel(run)

    finalize_usage(TEAM_A, run, 4_000, worker_id="passing-stranger")

    assert _tokens() != 4_000


def test_a_run_recovery_gave_up_on_keeps_its_last_owner(run):
    """The third route to (terminal, no worker): recovery past the attempt
    limit marks the run failed and nulls the worker, while that worker may
    still be walking its own exit path."""
    conn = _admin()
    try:
        conn.execute(
            "update public.agent_runs set worker_id='last-owner', attempts=3,"
            " status='running', lease_expires_at=now() - interval '1 hour'"
            " where id=%s", (run,))
        conn.execute("select public.recover_expired_agent_runs()")
        worker, status = conn.execute(
            "select worker_id, status from public.agent_runs where id=%s",
            (run,)).fetchone()
    finally:
        conn.close()
    assert (worker, status) == (None, "failed"), (worker, status)

    finalize_usage(TEAM_A, run, 70, worker_id="someone-else")
    assert _tokens() == CHUNK, "a stranger settled a run recovery gave up on"

    finalize_usage(TEAM_A, run, 900, worker_id="last-owner")
    assert _tokens() == 900


def test_ordinary_completion_is_unaffected(run):
    """The common path must not acquire a new way to fail."""
    conn = _admin()
    try:
        conn.execute("update public.agent_runs set worker_id='mine'"
                     " where id=%s", (run,))
    finally:
        conn.close()
    finish_run(TEAM_A, run, "done", input_tokens=600, output_tokens=100,
               worker_id="mine")

    finalize_usage(TEAM_A, run, 700, worker_id="mine")

    assert _tokens() == 700


def test_settlement_still_happens_only_once(run):
    """Whoever wins, they win once: a second settlement would hand the team
    back tokens it spent."""
    conn = _admin()
    try:
        conn.execute("update public.agent_runs set worker_id='mine'"
                     " where id=%s", (run,))
    finally:
        conn.close()
    finish_run(TEAM_A, run, "done", input_tokens=600, output_tokens=100,
               worker_id="mine")

    finalize_usage(TEAM_A, run, 700, worker_id="mine")
    finalize_usage(TEAM_A, run, 700, worker_id="mine")

    assert _tokens() == 700

