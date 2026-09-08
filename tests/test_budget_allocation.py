"""Two turns spending the same money.

🔴 THE DEFECT (fix.md F30). `run_allowance` handed every active run its own
reservation PLUS the team's whole unclaimed headroom, and a run's real spend
only reaches the bucket when it finalizes. So with a 500,000 cap and two turns
holding 6,000 reservations, the bucket reads 12,000 and each run is told it may
spend 494,000. Both can, and the team spends 980,000 against a 500,000 cap.

The in-run brake was never wrong about one turn. It was wrong that a shared
budget can be divided by reading it — the headroom has to be CLAIMED, and the
claim has to be the same kind of atomic conditional update that admission
already uses.
"""
import threading

import psycopg
import pytest

from shared.config import settings
from shared.usage import claim_budget, claimed_tokens
from tests._seed import A1, TEAM_A

CHUNK = settings.agent_tokens_estimate


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


def _bucket_tokens() -> int:
    conn = _admin()
    try:
        row = conn.execute(
            "select tokens from public.usage_buckets where team_id=%s"
            "  and bucket=date_trunc('hour', now())", (TEAM_A,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


def _run(reserved: int, *, thread_title: str | None = None) -> str:
    """A running turn. Its own thread when asked: the queue allows one active
    run per thread, so two concurrent runs need two."""
    conn = _admin()
    try:
        if thread_title:
            thread_id = conn.execute(
                "insert into public.threads (team_id, title, visibility, kind,"
                " created_by) values (%s,%s,'team','discussion',%s)"
                " returning id", (TEAM_A, thread_title, A1),
            ).fetchone()[0]
        else:
            thread_id = conn.execute(
                "select id from public.threads where team_id=%s"
                "  and title='General'", (TEAM_A,),
            ).fetchone()[0]
        return str(conn.execute(
            "insert into public.agent_runs (team_id, thread_id, requester_id,"
            " status, trigger_type, tokens_reserved) values"
            " (%s,%s,%s,'running','user',%s) returning id",
            (TEAM_A, thread_id, A1, reserved),
        ).fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------

def test_two_runs_cannot_both_claim_the_last_chunk(seeded):
    """🔴 They could — each read the same headroom and each believed it was
    theirs. Released together, on separate connections, because the defect is
    about what two transactions see of one row."""
    cap = settings.agent_tokens_per_hour
    # One chunk of room left in the hour.
    _bucket(cap - CHUNK)
    runs = [_run(CHUNK, thread_title="One"),
            _run(CHUNK, thread_title="Two")]

    barrier = threading.Barrier(2)
    granted: list[bool] = []
    lock = threading.Lock()

    def _try(run_id: str) -> None:
        barrier.wait(timeout=10)
        ok = claim_budget(TEAM_A, run_id)
        with lock:
            granted.append(ok)

    threads = [threading.Thread(target=_try, args=(r,)) for r in runs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert granted.count(True) == 1, (
        f"both runs were granted the last chunk: {granted}"
    )
    assert _bucket_tokens() <= cap, "the hour's cap was passed"


def test_a_granted_claim_is_charged_to_the_team(seeded):
    """A claim nobody pays for is not a claim."""
    _bucket(0)
    run_id = _run(CHUNK)

    assert claim_budget(TEAM_A, run_id) is True

    assert _bucket_tokens() == CHUNK


def test_a_granted_claim_raises_what_the_run_may_spend(seeded):
    _bucket(0)
    run_id = _run(CHUNK)
    before = claimed_tokens(TEAM_A, run_id)

    claim_budget(TEAM_A, run_id)

    assert claimed_tokens(TEAM_A, run_id) == before + CHUNK


def test_a_refused_claim_charges_nothing(seeded):
    """The refusal has to leave the bucket alone, or a run that is stopped
    still costs the team the money it was denied."""
    cap = settings.agent_tokens_per_hour
    _bucket(cap)
    run_id = _run(CHUNK)

    assert claim_budget(TEAM_A, run_id) is False

    assert _bucket_tokens() == cap


def test_repeated_claims_walk_the_bucket_up_to_the_cap_and_stop(seeded):
    """Many small claims must not add up past the cap — this is the whole
    property, stated without concurrency."""
    cap = settings.agent_tokens_per_hour
    _bucket(0)
    run_id = _run(0)

    granted = 0
    while claim_budget(TEAM_A, run_id):
        granted += 1
        assert granted < (cap // CHUNK) + 5, "the claim loop never refused"

    assert _bucket_tokens() <= cap
    assert granted == cap // CHUNK


def test_an_uncapped_team_is_always_granted(seeded, monkeypatch):
    monkeypatch.setattr(settings, "agent_tokens_per_hour", 0)
    run_id = _run(CHUNK)

    assert claim_budget(TEAM_A, run_id) is True


def test_claims_from_two_teams_do_not_compete(seeded):
    """A shared cap is per TEAM. Serialising one team's claims must not
    serialise the deployment."""
    from tests._seed import B1, TEAM_B

    conn = _admin()
    try:
        conn.execute(
            "insert into public.usage_buckets (team_id, bucket, turns, tokens)"
            " values (%s, date_trunc('hour', now()), 1, 0)"
            " on conflict (team_id, bucket) do update set tokens = 0",
            (TEAM_B,),
        )
        thread_b = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM_B,),
        ).fetchone()[0]
        run_b = str(conn.execute(
            "insert into public.agent_runs (team_id, thread_id, requester_id,"
            " status, trigger_type, tokens_reserved) values"
            " (%s,%s,%s,'running','user',%s) returning id",
            (TEAM_B, thread_b, B1, CHUNK),
        ).fetchone()[0])
    finally:
        conn.close()

    _bucket(settings.agent_tokens_per_hour)   # team A is out
    run_a = _run(CHUNK)

    assert claim_budget(TEAM_A, run_a) is False
    assert claim_budget(TEAM_B, run_b) is True
