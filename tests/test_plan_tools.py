"""A thread's plan is optional, versioned, and owned by the thread.

Task 13. The plan is working state, not an approval: nothing here goes to a
human, because nothing here leaves the thread. What it must not do is exist
before the agent asked for it, be written by one thread into another's row, or
be overwritten by a model working from a version it has not seen.
"""
import psycopg
import pytest

from agent.plan_tools import read_plan, update_plan
from agent.runtime import _continuation_content
from pipeline.parsers import spotlight
from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, TEAM_B, as_user


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def _general(admin, team_id=TEAM_A):
    return admin.execute(
        "select id from public.threads where team_id=%s and title='General'",
        (team_id,),
    ).fetchone()[0]


def _restricted(admin, *, participants):
    thread_id = admin.execute(
        "insert into public.threads (team_id, title, visibility, kind, work_state, created_by)"
        " values (%s,'A1 work','restricted','work','planned',%s) returning id",
        (TEAM_A, A1),
    ).fetchone()[0]
    for user_id in participants:
        admin.execute(
            "insert into public.thread_participants (thread_id, team_id, user_id, added_by)"
            " values (%s,%s,%s,%s)",
            (thread_id, TEAM_A, user_id, A1),
        )
    return thread_id


STEPS = [
    {"id": "1", "text": "read the failing test", "status": "completed"},
    {"id": "2", "text": "fix the parser", "status": "active"},
    {"id": "3", "text": "run the suite", "status": "pending"},
]


def test_a_thread_starts_with_no_plan(seeded, admin):
    """Creating a thread must not create a plan — the tool is optional, and a
    seeded empty plan would be an instruction to fill it in."""
    assert read_plan(TEAM_A, _general(admin)) is None


def test_the_first_update_creates_version_one(seeded, admin):
    result = update_plan(TEAM_A, _general(admin), STEPS)
    assert result["status"] == "ok"
    assert result["version"] == 1
    assert read_plan(TEAM_A, _general(admin))["steps"] == STEPS


def test_an_update_that_knows_the_current_version_wins(seeded, admin):
    thread_id = _general(admin)
    update_plan(TEAM_A, thread_id, STEPS)
    done = [dict(s, status="completed") for s in STEPS]
    result = update_plan(TEAM_A, thread_id, done, expected_version=1)
    assert result["status"] == "ok"
    assert result["version"] == 2
    assert read_plan(TEAM_A, thread_id)["steps"] == done


def test_a_stale_version_is_refused_and_reports_the_current_one(seeded, admin):
    """Two runs steering the same thread must not silently overwrite each
    other. The refusal carries the live plan so the caller can re-decide."""
    thread_id = _general(admin)
    update_plan(TEAM_A, thread_id, STEPS)
    update_plan(TEAM_A, thread_id, STEPS, expected_version=1)
    stale = update_plan(TEAM_A, thread_id, [], expected_version=1)
    assert stale["status"] == "conflict"
    assert stale["version"] == 2
    assert stale["steps"] == STEPS
    assert read_plan(TEAM_A, thread_id)["version"] == 2


def test_writing_a_first_plan_over_an_existing_one_is_a_conflict(seeded, admin):
    thread_id = _general(admin)
    update_plan(TEAM_A, thread_id, STEPS)
    assert update_plan(TEAM_A, thread_id, STEPS)["status"] == "conflict"


def test_two_active_steps_are_refused(seeded, admin):
    steps = [dict(s, status="active") for s in STEPS[:2]]
    result = update_plan(TEAM_A, _general(admin), steps)
    assert result["status"] == "invalid"
    assert read_plan(TEAM_A, _general(admin)) is None


def test_duplicate_step_ids_are_refused(seeded, admin):
    steps = [{"id": "1", "text": "a", "status": "pending"},
             {"id": "1", "text": "b", "status": "pending"}]
    assert update_plan(TEAM_A, _general(admin), steps)["status"] == "invalid"


def test_an_unknown_status_is_refused(seeded, admin):
    steps = [{"id": "1", "text": "a", "status": "nearly"}]
    assert update_plan(TEAM_A, _general(admin), steps)["status"] == "invalid"


def test_a_participant_sees_the_plan_and_an_outsider_does_not(seeded, admin):
    thread_id = _restricted(admin, participants=[A1])
    update_plan(TEAM_A, thread_id, STEPS)
    with as_user(A1) as conn:
        assert conn.execute(
            "select version from public.thread_plans where thread_id=%s", (thread_id,)
        ).fetchone() == (1,)
    with as_user(A2) as conn:
        assert conn.execute(
            "select version from public.thread_plans where thread_id=%s", (thread_id,)
        ).fetchone() is None


def test_a_team_thread_plan_is_visible_to_the_team(seeded, admin):
    thread_id = _general(admin)
    update_plan(TEAM_A, thread_id, STEPS)
    with as_user(A2) as conn:
        assert conn.execute(
            "select version from public.thread_plans where thread_id=%s", (thread_id,)
        ).fetchone() == (1,)
    with as_user(B1) as conn:
        assert conn.execute(
            "select version from public.thread_plans where thread_id=%s", (thread_id,)
        ).fetchone() is None


def test_a_plan_cannot_be_written_onto_another_teams_thread(seeded, admin):
    """The thread id is server-bound, so this is unreachable from the model —
    which is exactly why it must fail loudly rather than write a row whose
    team and thread disagree."""
    other = _general(admin, TEAM_B)
    # Named, not a bare psycopg.Error: a broad raises() is satisfied by any
    # breakage, including the schema being wrong, and would pass while the
    # constraint it claims to test did not exist.
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        update_plan(TEAM_A, other, STEPS)


def test_a_members_own_plan_write_is_refused(seeded, admin):
    """The plan is read-only to `authenticated`: the board (Task 14) moves a
    thread's work_state, not the agent's step list."""
    thread_id = _general(admin)
    update_plan(TEAM_A, thread_id, STEPS)
    with as_user(A1) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(
            "update public.thread_plans set steps='[]'::jsonb where thread_id=%s",
            (thread_id,),
        )


def test_the_continuation_record_carries_the_plan_and_its_unfinished_steps():
    """A restarted run reads its own plan back. Completed steps are omitted:
    what survives a restart is what is left to do.

    It arrives datamarked, like every other continuation record — a plan can
    be written from what a member asked for, so it is context to act on, not
    an instruction that outranks the rules.
    """
    content = _continuation_content([], {"version": 2, "steps": STEPS})
    text = content.parts[0].text
    assert spotlight("fix the parser") in text
    assert spotlight("run the suite") in text
    assert spotlight("read the failing test") not in text
    assert '"version":2' in text


def test_no_plan_and_no_effects_adds_nothing():
    assert _continuation_content([], None) is None


def test_the_tool_takes_its_thread_from_state_and_not_from_the_model(seeded, admin):
    """The model's parameters are steps and a version. Which thread the plan
    belongs to is bound server-side, so it cannot write into another one."""
    from types import SimpleNamespace

    from agent.plan_tools import plan_update

    thread_id = _general(admin)
    ctx = SimpleNamespace(state={"team_id": TEAM_A, "thread_id": thread_id})
    result = plan_update(STEPS, ctx)

    assert result == {"status": "ok", "version": 1, "steps": STEPS}
    assert read_plan(TEAM_A, thread_id)["steps"] == STEPS
