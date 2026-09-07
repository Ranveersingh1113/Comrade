"""What happens to a run while a person decides, and after they answer.

🔴 THE DEFECTS. A run parked on a consent card kept its WORKER LEASE — a
five-minute clock meant for detecting a dead process — while it waited on a
human, whose answer takes minutes to days. `recover_expired_agent_runs` then
treated the waiting run as an abandoned one, requeued it, and after three
cycles marked it `failed: worker lease expired`, all while the card sat there
pending and the member had done nothing wrong. Approving it afterwards
executed the action against a run that had already been declared dead.

And rejection resumed nothing at all. Approve and edit-and-approve both
requeued the waiting run; reject wrote its reason and stopped, so the run sat
in `waiting_for_permission` and the model never got to hear the "no" that
`resolution_reason` exists to deliver.
"""
import psycopg
import pytest

from agent.run_queue import claim_next_run, enqueue_turn, get_run, recover_expired_runs
from shared.config import settings
from shared.consent import propose_action, reject_consent
from tests._seed import A1, TEAM_A


def _thread_id() -> str:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        row = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM_A,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _park(run_id: str) -> None:
    """Park a claimed run the way the runtime does when a tool asks for consent."""
    from shared.agent_runs import pause_for_permission

    pause_for_permission(TEAM_A, run_id, None)


def _card(thread_id: str, run_id: str, title: str) -> str:
    return propose_action(
        TEAM_A, A1, "task_create",
        {"assignee_id": A1, "title": title, "description": None, "deadline": None},
        thread_id=thread_id, agent_run_id=run_id,
    )["consent_id"]


def _waiting_run() -> tuple[str, str]:
    thread_id = _thread_id()
    run_id = enqueue_turn(TEAM_A, A1, thread_id, "make me a task")
    run = claim_next_run("worker-one")
    assert run is not None and run.id == str(run_id)
    _park(run.id)
    return thread_id, run.id


def _status(run_id: str) -> str:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        return conn.execute(
            "select status from public.agent_runs where id=%s", (run_id,)
        ).fetchone()[0]


def test_a_run_waiting_on_a_person_holds_no_worker_lease(seeded):
    """The lease answers "is the worker alive?". Nobody is holding this run."""
    _, run_id = _waiting_run()

    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        lease, worker = conn.execute(
            "select lease_expires_at, worker_id from public.agent_runs where id=%s",
            (run_id,),
        ).fetchone()

    assert lease is None and worker is None


def test_recovery_does_not_reap_a_run_that_is_waiting_for_a_human(seeded):
    """A member reading a consent card for six minutes is not a crashed worker.

    This is the whole bug: recovery requeued the waiting run, the re-run
    proposed the same action, hit the pending card again, and parked again —
    three times, and then `failed: worker lease expired` with the card still
    pending."""
    thread_id, run_id = _waiting_run()
    _card(thread_id, run_id, "reading this carefully")
    # An hour of a member thinking about it, which is a perfectly ordinary
    # thing for a member to do.
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        conn.execute(
            "update public.agent_runs set created_at = now() - interval '1 hour'"
            " where id=%s",
            (run_id,),
        )
        conn.commit()

    recover_expired_runs()
    recover_expired_runs()

    assert _status(run_id) == "waiting_for_permission"


def test_rejecting_a_card_lets_the_run_hear_the_answer(seeded):
    """Rejection is an answer, and the model has to be resumed to receive it.

    `resolution_reason` is written so the agent can read WHY on its next turn
    instead of just THAT. There was no next turn: the run stayed parked."""
    thread_id, run_id = _waiting_run()
    consent_id = _card(thread_id, run_id, "not this one")

    assert reject_consent(TEAM_A, consent_id, A1, "wrong assignee")["status"] == "rejected"

    assert _status(run_id) == "queued"
    assert claim_next_run("worker-two").id == run_id


def test_a_card_nobody_ever_answers_ends_its_run(seeded):
    """With no lease there has to be some backstop, and the card's own expiry
    is the honest one: it is the clock that measures the thing being waited on."""
    thread_id, run_id = _waiting_run()
    consent_id = _card(thread_id, run_id, "abandoned")
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        conn.execute(
            "update public.consent_queue set expires_at = now() - interval '1 day'"
            " where id=%s",
            (consent_id,),
        )
        conn.commit()

    recover_expired_runs()

    assert _status(run_id) == "failed"
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        status, reason = conn.execute(
            "select status, resolution_reason from public.consent_queue where id=%s",
            (consent_id,),
        ).fetchone()
    assert status == "expired"
    assert reason, "the run must be able to say why it stopped"


def test_a_pending_card_keeps_its_run_parked(seeded):
    """The expiry sweep must not fail runs whose card is still answerable."""
    thread_id, run_id = _waiting_run()
    _card(thread_id, run_id, "still open")

    recover_expired_runs()

    assert _status(run_id) == "waiting_for_permission"


@pytest.mark.parametrize("resolution", ["approve", "reject"])
def test_the_resumed_run_is_the_same_logical_run(seeded, resolution):
    """Not a new one. The effect log, the input message and the thread's single
    active-run slot are all keyed to this id."""
    from shared.consent import approve_consent

    thread_id, run_id = _waiting_run()
    consent_id = _card(thread_id, run_id, f"resolve by {resolution}")
    before = get_run(TEAM_A, run_id)

    if resolution == "approve":
        approve_consent(TEAM_A, consent_id, A1)
    else:
        reject_consent(TEAM_A, consent_id, A1, "no thanks")

    after = get_run(TEAM_A, run_id)
    assert after is not None and before is not None
    assert after["id"] == before["id"]
    assert after["status"] == "queued"
