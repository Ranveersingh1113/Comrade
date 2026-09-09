"""Does Comrade actually ANSWER the questions it will be asked?

🔴 THE GAP (fix.md F52). The browser journey asks "What tasks are open right
now?" and accepts either an AI message or `[data-agent-note]`, because the model
returns nothing often enough that requiring a reply made the journey flaky. That
is correct coverage for "the member is told something either way" — and it means
a run where the agent answered NOTHING passes the suite. Nothing anywhere gated
the primary outcome, so a release could be green with the product silent.

These tests gate it. They are `live`: they make real Gemini calls, are
deselected by default, and are run deliberately.

WHAT MAKES AN ANSWER USEFUL HERE is a fact only the team's real state can
supply — a task title, a document's contents, a member's name — asserted
case-insensitively on substance rather than phrasing. Asserting on wording is
what made two earlier versions of the journey assertion depend on the model's
style; asserting on a seeded fact does not.

EMPTY TURNS ARE NOT CONCEALED. `agent/runtime.py` already retries an empty turn
three times inside a single turn; on top of that these tests will re-ASK, which
is what a member does, and the number of asks each prompt needed is printed. A
prompt that never answers fails. An error notice never counts as an answer.
"""
import os
import time

import psycopg
import pytest

from agent.runtime import run_turn_sync
from shared.config import settings
from tests._seed import A1, A2, TEAM_A, as_user

pytestmark = [pytest.mark.live, pytest.mark.skipif(
    not settings.gemini_api_key, reason="no GEMINI_API_KEY configured"
)]

#: How many times a prompt may be re-asked before it counts as unanswered.
#:
#: ONE. A member asks a question once, and the release acceptance is that they
#: get an answer — not that they get one if they ask twice.
#:
#: That is affordable because the retries that matter are INSIDE the turn.
#: Measured 2026-09-09 against the configured model, 42 calls: ~45% came back
#: with an empty candidate, so `agent.runtime.EMPTY_TURN_ATTEMPTS = 6` puts a
#: whole turn's chance of silence under 1%. Six internal attempts, one human
#: one.
#:
#: Overridable for diagnosis only. Raising it to get a green run would be
#: measuring a different product than the one a member uses, and the asks each
#: prompt needed are reported either way.
MAX_ASKS = int(os.environ.get("COMRADE_USEFULNESS_MAX_ASKS", "1"))

#: Facts nothing but the team's real state can supply.
TASK_TITLE = "Wire the telemetry exporter"
PAGE_TITLE = "Decisions"
PAGE_FACT = "The team mascot is a quokka"
PAGE_ANSWER = "quokka"


@pytest.fixture
def furnished(seeded):
    """A designated team with a live task and a wiki page to be asked about.

    "Open" is the member's word, not the schema's: the statuses are proposed,
    confirmed, in_progress and done. A task the team is actually carrying is
    `confirmed`.

    The wiki is three tables — a page, an entry on it, an active version
    carrying the fact — because that is what `memory_read_page` actually reads.
    Written out rather than assumed: the documents table turned out to be an
    uploaded-file model whose ids the agent has no way to discover from a
    question, which a fixture invented from the tool's docstring would have
    missed.
    """
    with psycopg.connect(settings.comrade_db_url_admin, autocommit=True) as conn:
        thread_id = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM_A,)).fetchone()[0]
        # Proposed first. `trg_tasks_confirm_guard` refuses a task that is born
        # live — "a new task must start as proposed and unconfirmed".
        task_id = conn.execute(
            "insert into public.tasks (team_id, assignee_id, title, status,"
            " created_by_kind, created_by_id, thread_id)"
            " values (%s,%s,%s,'proposed','user',%s,%s) returning id",
            (TEAM_A, A2, TASK_TITLE, A1, thread_id)).fetchone()[0]
        page_id = conn.execute(
            "insert into public.memory_pages (team_id, title, description, kind)"
            " values (%s,%s,'What the team has settled','fact')"
            " returning id", (TEAM_A, PAGE_TITLE)).fetchone()[0]
        entry_id = conn.execute(
            "insert into public.memory_entries (team_id, page_id)"
            " values (%s,%s) returning id", (TEAM_A, page_id)).fetchone()[0]
        conn.execute(
            "insert into public.memory_versions (entry_id, team_id, fact,"
            " change_type, is_active, valid_from)"
            " values (%s,%s,%s,'added',true,now())",
            (entry_id, TEAM_A, PAGE_FACT))

    # ...and confirmed BY ITS ASSIGNEE. The same guard refuses anyone else —
    # "only the assignee may confirm their own task" — including an admin
    # connection, which has no auth.uid() at all.
    with as_user(A2, commit=True) as member:
        member.execute(
            "update public.tasks set status='confirmed', confirmed_at=now()"
            " where id=%s", (task_id,))

    yield str(thread_id)


def _ask(thread_id: str, prompt: str) -> tuple[dict, int]:
    """Ask until answered, up to MAX_ASKS. Returns the outcome and asks used.

    ONLY an `empty` outcome is retried. `run_turn` tags its result with the
    frame that ended the turn, and the ones that arrive without a reply are not
    all the same event: `empty` is the transient this allowance exists for,
    while `waiting_for_permission` is the agent doing its job and parking an
    action for a person to answer.

    Re-asking the second one is not a retry, it is a second question into a
    thread the first is still holding — `one_active_agent_run_per_thread`
    refuses it, which is how this test found its own bug.
    """
    for ask in range(1, MAX_ASKS + 1):
        started = time.monotonic()
        result = run_turn_sync(TEAM_A, A1, prompt, thread_id=thread_id)
        elapsed = time.monotonic() - started
        result["_seconds"] = round(elapsed, 1)
        result["_asks"] = ask
        if (result.get("reply") or "").strip():
            return result, ask
        if "empty" not in result:
            return result, ask
        print(f"    [{prompt[:40]!r}] ask {ask}/{MAX_ASKS} empty after"
              f" {elapsed:.1f}s")
    return {"reply": "", "empty": True, "_seconds": 0.0, "_asks": MAX_ASKS}, MAX_ASKS


def _answers(thread_id: str, prompt: str, *must_contain: str) -> str:
    result, asks = _ask(thread_id, prompt)
    reply = (result.get("reply") or "").strip()
    print(f"    LATENCY {result.get('_seconds')}s  ASKS {asks}/{MAX_ASKS}"
          f"  -> {reply[:100]!r}")
    assert reply, (
        f"{prompt!r} produced no reply in {MAX_ASKS} asks. The member gets an"
        " error notice, which the browser journey accepts and this does not."
    )
    lowered = reply.lower()
    for fact in must_contain:
        assert fact.lower() in lowered, (
            f"{prompt!r} was answered without {fact!r}, so the reply is not"
            f" grounded in the team's state: {reply!r}"
        )
    return reply


def test_it_answers_a_task_lookup(furnished):
    """The judge prompt from the finding, gated on an actual answer."""
    _answers(furnished, "What tasks are open right now?", TASK_TITLE)


def test_it_answers_a_wiki_question(furnished):
    """Reading what the team wrote down, not reciting general knowledge. The
    fact is arbitrary on purpose: no model knows it without looking."""
    _answers(furnished, "What did we decide the team mascot would be?",
             PAGE_ANSWER)


def test_it_answers_a_team_context_question(furnished):
    """Who is here and what they are doing — the third judge prompt."""
    _answers(furnished, "Who is on this team and what is assigned to them?",
             "A2")


def test_a_consent_action_reaches_the_queue(furnished):
    """The action half. Proposing a task is a consent-gated effect, so a useful
    agent has to get as far as a card the member can answer — and no further
    without them.

    Asserted on the QUEUE, not on the reply: what matters is that the effect was
    staged for a person, not how the model described doing it.
    """
    before = _consent_count()

    result, asks = _ask(
        furnished,
        "Please propose a new task called 'Book the demo room' for A2.")

    assert "empty" not in result, (
        f"the model said nothing in {asks} asks, so no action was staged either"
    )
    # The turn may end with a reply or by parking on the card it just wrote;
    # both are the agent having done the work. What must NOT have happened is
    # the effect landing without a person, or not landing at all.
    assert _consent_count() > before, (
        "the agent finished without staging a consent item, so nothing it may"
        f" have said it would do can actually happen: {result!r}"
    )
    assert _tasks_named("Book the demo room") == 0, (
        "the task was created outright instead of being proposed — the consent"
        " gate is the product's whole promise"
    )


def _tasks_named(title: str) -> int:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        return conn.execute(
            "select count(*) from public.tasks where team_id=%s and title=%s",
            (TEAM_A, title)).fetchone()[0]


def _consent_count() -> int:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        return conn.execute(
            "select count(*) from public.consent_queue where team_id=%s",
            (TEAM_A,)).fetchone()[0]
