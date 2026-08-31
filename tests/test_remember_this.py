"""A member can tell Comrade to remember something.

findings §20.7.1: every fact in Comrade arrives through automatic extraction,
and a member who watches the compiler miss something important has no recourse.
Worth building for two reasons the section states plainly — it is the
highest-signal fact in the system, because a human explicitly marked it, and it
is the only available mitigation for extractor starvation (§20.3.2), stage 1
being otherwise the sole path from source to memory.

THE SOLE-WRITER CONSTRAINT IMPROVES THE FEATURE
------------------------------------------------
Members cannot write memory_* — RLS grants that to comrade_pipeline alone
(§6.0). So this cannot be "insert a fact", and should not be: a direct write
would open an unspotlighted path straight into agent context. It enqueues a
compile instead, and the fact earns its citation, diff card and revert like
anything else.

Which makes the implementation almost nothing. The member's message is already
a `messages` row, so remembering it is a chat compile of exactly ONE message,
run now instead of at the five-message debounce. No new job type, no new
pipeline, no new citation path — and the citation the DB trigger demands is the
message id itself.
"""
import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, as_user


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def message(admin):
    return str(admin.execute(
        "insert into public.messages (team_id, thread_type, sender_kind,"
        " sender_id, body) values (%s,'group','user',%s,"
        " 'the listing expiry is 90 minutes, that is not negotiable')"
        " returning id",
        (TEAM_A, A1),
    ).fetchone()[0])


def _remember(message_id, user_id=A1, team_id=TEAM_A):
    from server.app import remember_message

    return remember_message(team_id, message_id, user_id)


def test_remembering_queues_a_compile_of_that_one_message(seeded, admin, message):
    result = _remember(message)
    payload = admin.execute(
        "select payload from public.jobs where id=%s", (result["job_id"],)
    ).fetchone()[0]
    assert payload["message_ids"] == [message]


def test_it_does_not_move_the_chat_watermark(seeded, admin, message):
    """The bug this would ship without care.

    A chat compile records `chat_through`, and chat_watermark takes the max
    across done compilations — so stamping this one with the message's
    timestamp would advance the sweep past every message the sweep has not read
    yet. Remembering one line would silently discard the surrounding
    conversation, which is the opposite of what a member asking to remember
    something wants.
    """
    payload = admin.execute(
        "select payload from public.jobs where team_id=%s and job_type='compile_memory'"
        " order by created_at desc limit 1",
        (TEAM_A,),
    ).fetchone()
    _remember(message)
    payload = admin.execute(
        "select payload from public.jobs where team_id=%s and job_type='compile_memory'"
        " order by created_at desc limit 1",
        (TEAM_A,),
    ).fetchone()[0]
    assert payload.get("through") is None


def test_the_compilation_records_that_a_human_asked(seeded, admin, message):
    """§20.7.1 calls this the highest-signal fact in the system precisely
    because a person marked it. A compilation indistinguishable from the
    automatic sweep throws that signal away."""
    from pipeline.chat import handle_chat_compile_job

    result = _remember(message)
    payload = admin.execute(
        "select payload from public.jobs where id=%s", (result["job_id"],)
    ).fetchone()[0]
    handle_chat_compile_job(TEAM_A, payload)

    trigger = admin.execute(
        "select trigger from public.memory_compilations where team_id=%s"
        " order by started_at desc limit 1",
        (TEAM_A,),
    ).fetchone()[0]
    assert trigger == "on_demand"


def test_asking_twice_is_one_job(seeded, admin, message):
    """A double tap, or two members marking the same line, is one request."""
    first = _remember(message)
    second = _remember(message, user_id=A2)
    assert first["job_id"] == second["job_id"]
    assert admin.execute(
        "select count(*) from public.jobs where team_id=%s"
        " and job_type='compile_memory'",
        (TEAM_A,),
    ).fetchone()[0] == 1


def test_a_non_member_cannot_remember_into_your_team(seeded, message):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _remember(message, user_id=B1)
    assert exc.value.status_code == 403


def test_a_message_from_another_team_is_refused(seeded, admin):
    """team_id comes from the route and message_id from the body of a click.

    Read as the caller, so RLS already hides another team's message — but the
    refusal has to be explicit, or a mismatched pair would enqueue a job whose
    message list silently resolves to nothing and compiles an empty batch.
    """
    from fastapi import HTTPException
    from tests._seed import TEAM_B

    other = str(admin.execute(
        "insert into public.messages (team_id, thread_type, sender_kind,"
        " sender_id, body) values (%s,'group','user',%s,'team B says hello')"
        " returning id",
        (TEAM_B, B1),
    ).fetchone()[0])
    with pytest.raises(HTTPException) as exc:
        _remember(other, user_id=A1, team_id=TEAM_A)
    assert exc.value.status_code == 404


def test_a_private_message_cannot_be_remembered(seeded, admin):
    """§6.0: private threads never reach memory. Remembering is a member
    action, and a member must not be able to route their own private thread
    into the shared wiki by tapping a button — that is the one boundary the
    whole memory design rests on.
    """
    from fastapi import HTTPException

    private = str(admin.execute(
        "insert into public.messages (team_id, thread_type, thread_owner_id,"
        " sender_kind, sender_id, body) values (%s,'private',%s,'user',%s,"
        " 'something I only told Comrade') returning id",
        (TEAM_A, A1, A1),
    ).fetchone()[0])
    with pytest.raises(HTTPException) as exc:
        _remember(private, user_id=A1)
    assert exc.value.status_code == 404


def test_a_deleted_message_cannot_be_remembered(seeded, admin, message):
    """A tombstoned message is retracted. Compiling it into memory would put
    the retracted text back in front of everyone, permanently, with a
    citation."""
    from fastapi import HTTPException

    admin.execute(
        "update public.messages set deleted_scope='everyone', deleted_by=%s,"
        " deleted_at=now() where id=%s",
        (A1, message),
    )
    with pytest.raises(HTTPException) as exc:
        _remember(message)
    assert exc.value.status_code == 404


@pytest.mark.live
def test_a_remembered_fact_lands_in_the_wiki_with_its_citation(seeded, admin, message):
    """End to end against the real model, because the value of this feature is
    that the fact arrives like any other — earning a citation, and therefore a
    diff card and a revert."""
    from pipeline.chat import handle_chat_compile_job

    result = _remember(message)
    payload = admin.execute(
        "select payload from public.jobs where id=%s", (result["job_id"],)
    ).fetchone()[0]
    handle_chat_compile_job(TEAM_A, payload)

    rows = admin.execute(
        "select v.fact, c.source_kind, c.source_id"
        " from public.memory_versions v"
        " join public.memory_citations c on c.version_id = v.id"
        " where v.team_id=%s and v.is_active and c.source_id=%s",
        (TEAM_A, message),
    ).fetchall()
    assert rows, "the remembered message produced no cited fact"
    assert all(kind == "message" for _, kind, _ in rows)
    assert any("90" in fact for fact, _, _ in rows)
