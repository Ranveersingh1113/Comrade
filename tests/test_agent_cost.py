"""What a turn cost, in columns that existed and were never written.

`agent_runs.input_tokens`, `output_tokens` and `cost_usd` have been in the
schema since the table was created and nothing ever set them. So every question
about what the agent actually spends — which teams, which kinds of turn,
whether the empty-turn retry matters — had no data behind it, and the only
budget in the product counted TURNS, which treats "what's my status" and a turn
that reads twenty files as the same thing.

TOKENS ARE A FACT, COST IS A POLICY. The counts are recorded unconditionally.
The price is not: it changes, it differs per deployment, and a confidently
wrong number in a money column is worse than a null.
"""
import psycopg
import pytest

from agent.runtime import _usage_from_event
from shared.agent_runs import _cost_usd, finish_run, start_run
from shared.config import settings
from tests._seed import A1, TEAM_A


def _start(team_id: str, summary: str) -> str:
    conn = psycopg.connect(settings.comrade_db_url_admin)
    try:
        row = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (team_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return start_run(team_id, A1, str(row[0]), None, "user", summary)


class _Usage:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _Event:
    def __init__(self, usage=None):
        if usage is not None:
            self.usage_metadata = usage


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reading the vendor's numbers
# ---------------------------------------------------------------------------

def test_prompt_and_generated_tokens_are_read():
    usage = _Usage(prompt_token_count=3180, candidates_token_count=214,
                   total_token_count=3394)
    assert _usage_from_event(_Event(usage)) == (3180, 214)


def test_generated_tokens_fall_back_to_the_difference():
    """The empty-turn instrumentation saw exactly this shape: a prompt count
    and a total, with no candidates count, because there were no candidates."""
    usage = _Usage(prompt_token_count=3180, candidates_token_count=None,
                   total_token_count=3180)
    assert _usage_from_event(_Event(usage)) == (3180, 0)


def test_an_event_with_no_usage_is_zero_not_an_error():
    """🔴 Accounting must never fail a turn that produced work.

    This reads a field off a vendor SDK object. If the shape changes, the
    right outcome is an understated number, not a member's answer disappearing
    into a traceback.
    """
    assert _usage_from_event(_Event()) == (0, 0)
    assert _usage_from_event(object()) == (0, 0)


def test_a_garbled_usage_object_does_not_raise():
    assert _usage_from_event(_Event(_Usage())) == (0, 0)


# ---------------------------------------------------------------------------
# Cost: absent unless somebody said what it costs
# ---------------------------------------------------------------------------

def test_no_configured_rate_means_no_cost_not_a_free_turn(monkeypatch):
    """🔴 Zero is a price.

    A cost column full of 0.00 reads as "this was free" rather than "nobody
    configured this", and the difference matters the first time somebody adds
    up a month.
    """
    monkeypatch.setattr("shared.config.settings.gemini_input_usd_per_mtok", 0.0)
    monkeypatch.setattr("shared.config.settings.gemini_output_usd_per_mtok", 0.0)
    assert _cost_usd(10_000, 2_000) is None


def test_a_configured_rate_is_applied_per_million(monkeypatch):
    monkeypatch.setattr("shared.config.settings.gemini_input_usd_per_mtok", 0.30)
    monkeypatch.setattr("shared.config.settings.gemini_output_usd_per_mtok", 2.50)
    # 1M input at $0.30 + 1M output at $2.50
    assert _cost_usd(1_000_000, 1_000_000) == pytest.approx(2.80)
    assert _cost_usd(0, 0) == 0.0


# ---------------------------------------------------------------------------
# It reaches the row
# ---------------------------------------------------------------------------

def test_finish_run_records_what_the_turn_used(seeded, admin, monkeypatch):
    monkeypatch.setattr("shared.config.settings.gemini_input_usd_per_mtok", 0.30)
    monkeypatch.setattr("shared.config.settings.gemini_output_usd_per_mtok", 2.50)
    run_id = _start(TEAM_A, "how are the tasks going")
    finish_run(TEAM_A, run_id, "done", input_tokens=12_000, output_tokens=800)

    row = admin.execute(
        "select status, input_tokens, output_tokens, cost_usd"
        " from public.agent_runs where id = %s", (run_id,)
    ).fetchone()
    assert row[0] == "done"
    assert row[1] == 12_000
    assert row[2] == 800
    assert float(row[3]) == pytest.approx((12_000 * 0.30 + 800 * 2.50) / 1e6)


def test_a_failed_turn_still_records_what_it_burned(seeded, admin):
    """🔴 Accounting that only counts successes understates exactly the runs
    worth investigating.

    A turn that failed still consumed the prompt it was handed — and a run
    that failed after three empty-turn retries is the most expensive kind
    there is.
    """
    run_id = _start(TEAM_A, "a turn that will fail")
    finish_run(TEAM_A, run_id, "failed", input_tokens=9_540, output_tokens=0)

    row = admin.execute(
        "select status, input_tokens from public.agent_runs where id = %s",
        (run_id,),
    ).fetchone()
    assert row == ("failed", 9_540)


def test_usage_defaults_to_zero_so_old_callers_still_work(seeded, admin):
    """finish_run's usage arguments are optional: a caller that has nothing to
    report writes zeros rather than failing, and the column stops being null
    for reasons nobody can distinguish from 'never ran'."""
    run_id = _start(TEAM_A, "no usage reported")
    finish_run(TEAM_A, run_id, "done")
    row = admin.execute(
        "select input_tokens, output_tokens from public.agent_runs where id = %s",
        (run_id,),
    ).fetchone()
    assert row == (0, 0)


# ---------------------------------------------------------------------------
# The budget that uses them
# ---------------------------------------------------------------------------

def _spend(admin, team_id, *, turns=1, tokens_each=0):
    for i in range(turns):
        admin.execute(
            "insert into public.agent_runs"
            " (team_id, trigger_type, input_summary, status,"
            "  input_tokens, output_tokens)"
            " values (%s,'user',%s,'done',%s,0)",
            (team_id, f"seeded {i}", tokens_each),
        )


def test_the_budget_binds_on_tokens_not_only_on_turns(seeded, admin, monkeypatch):
    """🔴 Counting turns is a turnstile, not a budget.

    A measured trivial turn costs ~5,100 input tokens before the member types
    a word — system prompt, wiki index, 19 tool declarations — and a turn that
    reads twenty files costs orders more. So "60 turns" is anywhere between
    300K and several million tokens depending entirely on what was asked, and
    the run worth bounding is exactly the one that slips through.
    """
    from fastapi import HTTPException

    from server.app import _check_turn_budget

    monkeypatch.setattr("shared.config.settings.agent_turns_per_hour", 1000)
    monkeypatch.setattr("shared.config.settings.agent_tokens_per_hour", 50_000)
    admin.execute("delete from public.agent_runs where team_id=%s", (TEAM_A,))

    _spend(admin, TEAM_A, turns=5, tokens_each=9_000)  # 45K — under
    _check_turn_budget(TEAM_A)

    _spend(admin, TEAM_A, turns=1, tokens_each=9_000)  # 54K — over
    with pytest.raises(HTTPException) as caught:
        _check_turn_budget(TEAM_A)
    assert caught.value.status_code == 429
    # The message must name the dimension that bound. "You have used your
    # turns" when a team is out of TOKENS sends someone to the wrong knob.
    assert "tokens" in caught.value.detail
    admin.execute("delete from public.agent_runs where team_id=%s", (TEAM_A,))


def test_the_turn_cap_still_binds_on_cheap_turns(seeded, admin, monkeypatch):
    """Tokens alone would let a thousand near-empty turns through. Both
    dimensions, whichever comes first."""
    from fastapi import HTTPException

    from server.app import _check_turn_budget

    monkeypatch.setattr("shared.config.settings.agent_turns_per_hour", 3)
    monkeypatch.setattr("shared.config.settings.agent_tokens_per_hour", 10_000_000)
    admin.execute("delete from public.agent_runs where team_id=%s", (TEAM_A,))

    _spend(admin, TEAM_A, turns=3, tokens_each=10)
    with pytest.raises(HTTPException) as caught:
        _check_turn_budget(TEAM_A)
    assert "turns" in caught.value.detail
    admin.execute("delete from public.agent_runs where team_id=%s", (TEAM_A,))


def test_null_token_columns_do_not_poison_the_sum(seeded, admin, monkeypatch):
    """🔴 Rows predating this accounting have NULL token columns, and
    `sum()` over a NULL is NULL — which would compare as neither over nor
    under and silently disable the cap for any team with one old run."""
    from server.app import _check_turn_budget

    monkeypatch.setattr("shared.config.settings.agent_tokens_per_hour", 50_000)
    admin.execute("delete from public.agent_runs where team_id=%s", (TEAM_A,))
    admin.execute(
        "insert into public.agent_runs (team_id, trigger_type, input_summary,"
        " status, input_tokens, output_tokens) values (%s,'user','old',"
        " 'done', null, null)",
        (TEAM_A,),
    )
    _check_turn_budget(TEAM_A)  # must not raise, must not crash
    admin.execute("delete from public.agent_runs where team_id=%s", (TEAM_A,))
