"""The hourly cap has to bind under concurrency, or it is not a cap.

🔴 What this replaces: `_check_turn_budget` ran `count(*)` and `sum(tokens)`
over agent_runs and compared them to the setting. The comparison and the spend
were different transactions, so two turns reading the same snapshot both
passed — and on a deployment where the model key belongs to the operator, an
advisory cap is an unbounded bill.

Every test here spends through the real statement rather than a mock, because
the whole claim is about what Postgres does when two of them arrive at once.
"""
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from shared.config import settings
from shared.usage import (
    BudgetExceeded, finalize_usage, record_reservation, release_turn,
    reserve_turn,
)
from tests._seed import A1, TEAM_A, TEAM_B


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def caps(monkeypatch):
    """Small caps, so a test spends a whole budget in a few calls."""
    monkeypatch.setattr("shared.config.settings.agent_turns_per_hour", 3)
    monkeypatch.setattr("shared.config.settings.agent_tokens_per_hour", 30_000)
    monkeypatch.setattr("shared.config.settings.agent_tokens_estimate", 6_000)


def _bucket(admin, team_id=TEAM_A):
    return admin.execute(
        "select turns, tokens from public.usage_buckets"
        " where team_id=%s and bucket=date_trunc('hour', now())",
        (team_id,),
    ).fetchone()


def _run(admin, team_id=TEAM_A):
    thread_id = admin.execute(
        "select id from public.threads where team_id=%s and title='General'",
        (team_id,),
    ).fetchone()[0]
    return admin.execute(
        "insert into public.agent_runs"
        " (team_id, thread_id, requester_id, status, trigger_type)"
        " values (%s,%s,%s,'running','user') returning id",
        (team_id, thread_id, A1),
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Spending
# ---------------------------------------------------------------------------

def test_a_reservation_is_recorded_against_the_hour(seeded, caps, admin):
    assert reserve_turn(TEAM_A) == 6_000
    assert _bucket(admin) == (1, 6_000)


def test_the_turn_cap_binds(seeded, caps, admin):
    for _ in range(3):
        reserve_turn(TEAM_A)
    with pytest.raises(BudgetExceeded, match="turns"):
        reserve_turn(TEAM_A)
    assert _bucket(admin) == (3, 18_000), "a refused turn must spend nothing"


def test_the_token_cap_binds_before_the_turn_cap_when_turns_are_expensive(
    seeded, caps, admin
):
    """Whichever binds first wins, and the message says which — "you have used
    your turns" sent to a team that is out of TOKENS points at the wrong
    thing."""
    reserve_turn(TEAM_A, 25_000)
    with pytest.raises(BudgetExceeded, match="tokens"):
        reserve_turn(TEAM_A, 25_000)


def test_concurrent_turns_cannot_overshoot_the_cap(seeded, caps, admin):
    """🔴 The defect itself. Eight simultaneous admissions against a cap of
    three: under the old count-then-insert every one of them read a snapshot
    below the cap and proceeded."""
    def _try():
        try:
            reserve_turn(TEAM_A)
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        granted = sum(pool.map(lambda _: _try(), range(8)))

    assert granted == 3, f"{granted} turns admitted against a cap of 3"
    assert _bucket(admin) == (3, 18_000)


def test_one_teams_spending_does_not_bind_another(seeded, caps, admin):
    for _ in range(3):
        reserve_turn(TEAM_A)
    reserve_turn(TEAM_B)
    assert _bucket(admin, TEAM_B) == (1, 6_000)


# ---------------------------------------------------------------------------
# Giving it back
# ---------------------------------------------------------------------------

def test_a_turn_that_never_started_releases_what_it_took(seeded, caps, admin):
    reserve_turn(TEAM_A)
    release_turn(TEAM_A, 6_000)
    assert _bucket(admin) == (0, 0)


def test_releasing_never_earns_budget(seeded, caps, admin):
    """A bug in the accounting must not become a way to mint turns."""
    reserve_turn(TEAM_A)
    for _ in range(5):
        release_turn(TEAM_A, 6_000)
    assert _bucket(admin) == (0, 0)


def test_finalizing_replaces_the_estimate_with_the_truth(seeded, caps, admin):
    run_id = _run(admin)
    reserve_turn(TEAM_A)
    record_reservation(TEAM_A, str(run_id), 6_000)
    finalize_usage(TEAM_A, str(run_id), 1_200)
    assert _bucket(admin) == (1, 1_200)


def test_an_expensive_turn_is_charged_what_it_cost(seeded, caps, admin):
    run_id = _run(admin)
    reserve_turn(TEAM_A)
    record_reservation(TEAM_A, str(run_id), 6_000)
    finalize_usage(TEAM_A, str(run_id), 20_000)
    assert _bucket(admin) == (1, 20_000)


def test_finalizing_twice_changes_nothing(seeded, caps, admin):
    """The run finishes on the success path, the failure path and the
    cancellation path. A second reconciliation would hand out free budget."""
    run_id = _run(admin)
    reserve_turn(TEAM_A)
    record_reservation(TEAM_A, str(run_id), 6_000)
    finalize_usage(TEAM_A, str(run_id), 1_200)
    finalize_usage(TEAM_A, str(run_id), 1_200)
    assert _bucket(admin) == (1, 1_200)


def test_a_failed_turn_is_still_charged_for_its_prompt(seeded, caps, admin):
    """Accounting that only counts successes understates exactly the runs
    worth investigating."""
    run_id = _run(admin)
    reserve_turn(TEAM_A)
    record_reservation(TEAM_A, str(run_id), 6_000)
    finalize_usage(TEAM_A, str(run_id), 3_100)
    assert _bucket(admin) == (1, 3_100)


def test_no_caps_configured_spends_nothing(seeded, monkeypatch, admin):
    monkeypatch.setattr("shared.config.settings.agent_turns_per_hour", 0)
    monkeypatch.setattr("shared.config.settings.agent_tokens_per_hour", 0)
    reserve_turn(TEAM_A)
    assert _bucket(admin) is None
