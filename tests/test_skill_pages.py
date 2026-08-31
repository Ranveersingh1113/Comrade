"""A wiki page can hold a procedure, not just facts.

findings §6.3-3 lists the gap plainly — *"facts only — no skills, no procedural
memory"* — and §24.2 promotes it from optional layering to the thing that
matters: a team with no manager loses the handoff, and *"a standard is a
playbook everyone knows cold before they sit down."* A standard IS procedural
memory. That is the whole argument for this column.

WHAT THIS DELIBERATELY DOES NOT SHIP: `memory_pages.scope`
-----------------------------------------------------------
The Phase 4 plan paired `kind` with a `scope` column, and reading §4.5 closely
says not to. §4.5's advice is conditional — *if* you build private memory, make
it a scope on the same pages rather than a parallel store. It is not a
requirement to build private memory now, and §6.2-1 parked the persona layer on
purpose.

Today every page is team-visible by construction: private threads never reach
memory (§6.0), so there is no second scope with anything in it. A `scope`
column would be a column that is always 'team', and `restricted` would be a
value with no enforcement behind it — this schema has no notion of "some
members", so the word would mean nothing while looking like it meant something.
That is worse than no column.

§4.5's guidance is preserved by NOT pre-empting it: when personal memory earns
its way in, it arrives here as a scope on these pages, not as a parallel store.
"""
import psycopg
import pytest

from pipeline.compiler import Candidate, Decision, validate_decisions
from shared.config import settings
from tests._seed import TEAM_A


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The column
# ---------------------------------------------------------------------------

def test_a_page_is_a_fact_page_unless_it_says_otherwise(seeded, admin):
    """Every existing page predates this column, so the default has to be the
    old behaviour — a backfill that guessed would relabel a team's whole wiki
    on a migration."""
    page_id = admin.execute(
        "insert into public.memory_pages (team_id, title) values (%s,'Deadlines')"
        " returning id",
        (TEAM_A,),
    ).fetchone()[0]
    assert admin.execute(
        "select kind from public.memory_pages where id=%s", (page_id,)
    ).fetchone()[0] == "fact"


def test_only_the_two_kinds_are_accepted(seeded, admin):
    """'skill' and 'fact' are the vocabulary. An unconstrained text column
    would let a typo create a third kind that renders nowhere."""
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute(
            "insert into public.memory_pages (team_id, title, kind)"
            " values (%s,'Bad','procedure')",
            (TEAM_A,),
        )


def test_a_skill_page_can_be_created(seeded, admin):
    page_id = admin.execute(
        "insert into public.memory_pages (team_id, title, kind)"
        " values (%s,'Releasing','skill') returning id",
        (TEAM_A,),
    ).fetchone()[0]
    assert admin.execute(
        "select kind from public.memory_pages where id=%s", (page_id,)
    ).fetchone()[0] == "skill"


def test_scope_was_deliberately_not_added(seeded, admin):
    """Pinning an absence, which is unusual and on purpose.

    The plan called for `scope` and this argues it out; without a test the next
    reader sees only a missing column and assumes it was forgotten. If a real
    need arrives, delete this test in the same commit that adds the column —
    that is the point, it forces the reasoning to be revisited rather than
    silently overridden.
    """
    cols = {
        r[0] for r in admin.execute(
            "select column_name from information_schema.columns"
            " where table_schema='public' and table_name='memory_pages'"
        ).fetchall()
    }
    assert "scope" not in cols, (
        "a scope column arrived — if that is deliberate, remove this test and"
        " say what 'restricted' now means and what enforces it"
    )
    assert "kind" in cols


# ---------------------------------------------------------------------------
# Consolidation can put a candidate on a skill page
# ---------------------------------------------------------------------------

def test_a_decision_may_name_the_kind_of_page_it_creates():
    """Without this the model can only ever create fact pages, and a skill
    page could exist but never be born — the column would be decoration."""
    kept = validate_decisions(
        [Candidate(text="always run the migration before deploying")],
        [],
        [Decision(candidate_index=0, action="add", page_title="Deploying",
                  page_kind="skill")],
    )
    assert kept[0].page_kind == "skill"


def test_an_unknown_kind_degrades_to_fact_rather_than_failing():
    """validate_decisions' whole contract: anything malformed degrades, never
    raises. A model inventing 'procedure' must not abort a compile — the fact
    still belongs in memory, just on an ordinary page."""
    kept = validate_decisions(
        [Candidate(text="x")],
        [],
        [Decision(candidate_index=0, action="add", page_title="P",
                  page_kind="procedure")],
    )
    assert kept[0].page_kind == "fact"


def test_a_decision_with_no_kind_is_a_fact_page():
    kept = validate_decisions(
        [Candidate(text="x")],
        [],
        [Decision(candidate_index=0, action="add", page_title="P")],
    )
    assert kept[0].page_kind == "fact"


# ---------------------------------------------------------------------------
# It has to reach the reader, or it is a column nobody sees
# ---------------------------------------------------------------------------

def test_the_rendered_wiki_marks_a_skill_page_as_a_procedure(seeded, admin):
    """§20.3.1's lesson applied to a second column: memory that is stored and
    then dropped at the render boundary may as well not exist. A skill page
    that renders identically to a fact page has changed nothing for the agent
    reading it."""
    from pipeline.wiki import render_team_wiki

    page_id = admin.execute(
        "insert into public.memory_pages (team_id, title, kind, description)"
        " values (%s,'Releasing','skill','how we ship') returning id",
        (TEAM_A,),
    ).fetchone()[0]
    entry_id = admin.execute(
        "insert into public.memory_entries (team_id, page_id) values (%s,%s)"
        " returning id",
        (TEAM_A, page_id),
    ).fetchone()[0]
    admin.execute(
        "insert into public.memory_versions (entry_id, team_id, fact, change_type)"
        " values (%s,%s,'run the migration before deploying','added')",
        (entry_id, TEAM_A),
    )

    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        text = render_team_wiki(conn, TEAM_A)

    assert "run the migration before deploying" in text
    assert "Releasing" in text
    # The distinction the agent needs: this is how the team does something, not
    # a fact about the project.
    assert "procedure" in text.lower()


def test_a_skill_decision_actually_creates_a_skill_page(seeded, admin):
    """The write path, which is where a carried-but-unused field hides.

    validate_decisions returning page_kind='skill' proves nothing on its own —
    if apply_compilation drops it, every page created is still a fact page and
    the column can never be anything else in practice.
    """
    from pipeline.compiler import _resolve_page

    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        page_id = _resolve_page(conn, TEAM_A, "Deploying", "how we ship", "skill")
        conn.commit()
    assert admin.execute(
        "select kind from public.memory_pages where id=%s", (page_id,)
    ).fetchone()[0] == "skill"


def test_an_existing_page_keeps_its_kind(seeded, admin):
    """One document must not be able to redefine what a page IS.

    The facts already on a fact page were written under that reading; flipping
    it to a procedure because a later candidate looked instructional would
    silently reinterpret all of them.
    """
    from pipeline.compiler import _resolve_page

    admin.execute(
        "insert into public.memory_pages (team_id, title, kind)"
        " values (%s,'Deadlines','fact')",
        (TEAM_A,),
    )
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        page_id = _resolve_page(conn, TEAM_A, "Deadlines", None, "skill")
        conn.commit()
    assert admin.execute(
        "select kind from public.memory_pages where id=%s", (page_id,)
    ).fetchone()[0] == "fact"


def test_a_fact_page_still_renders_as_bullets(seeded, admin):
    """The default path must be untouched — a numbered list on every page
    would be a formatting change dressed up as a feature."""
    from pipeline.wiki import render_team_wiki

    page_id = admin.execute(
        "insert into public.memory_pages (team_id, title) values (%s,'Deadlines')"
        " returning id",
        (TEAM_A,),
    ).fetchone()[0]
    entry_id = admin.execute(
        "insert into public.memory_entries (team_id, page_id) values (%s,%s)"
        " returning id",
        (TEAM_A, page_id),
    ).fetchone()[0]
    admin.execute(
        "insert into public.memory_versions (entry_id, team_id, fact, change_type)"
        " values (%s,%s,'the demo is on 14 march','added')",
        (entry_id, TEAM_A),
    )
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        text = render_team_wiki(conn, TEAM_A)
    assert "- the demo is on 14 march" in text
    assert "(procedure)" not in text
