"""Read-side wiki projection over the compiled memory.

Pages group facts into topics. This module is pure read/projection:
  - all_active_pages(): pages with their active facts — the compiler's
    consolidation context AND the source for rendering.
  (The recall index is NOT here: agent/agent.py:wiki_section builds the
  titles+descriptions projection inline and injects it into the system prompt
  every turn. page_index() was a duplicate of that and was deleted 2026-08-29.)
  - render_team_wiki(): the member-facing markdown "what the AI knows".

All queries run on a caller-provided team_session connection, so RLS confines
them to the one team; team_id in SQL is belt-and-braces. Nothing here writes.
"""

ORPHAN_TITLE = "Uncategorized"


def all_active_pages(conn, team_id: str) -> list[dict]:
    """Every page with its active, unarchived facts. Entries without a page
    (pre-page data) surface under a virtual 'Uncategorized' bucket so they
    stay visible to consolidation and rendering."""
    page_rows = conn.execute(
        "select id, title, description, kind from public.memory_pages"
        " where team_id = %s order by title",
        (team_id,),
    ).fetchall()
    fact_rows = conn.execute(
        "select e.page_id, e.id, v.fact, v.valid_from,"
        "       (select c.source_kind from public.memory_citations c"
        "         where c.version_id = v.id order by c.created_at limit 1)"
        " from public.memory_entries e"
        " join public.memory_versions v on v.entry_id = e.id and v.is_active"
        " where e.team_id = %s and not e.archived order by v.created_at",
        (team_id,),
    ).fetchall()

    by_page: dict[str, list[dict]] = {}
    orphans: list[dict] = []
    for page_id, entry_id, fact, valid_from, source_kind in fact_rows:
        item = {
            "entry_id": str(entry_id),
            "text": fact,
            "valid_from": valid_from,
            "source_kind": source_kind,
        }
        if page_id is None:
            orphans.append(item)
        else:
            by_page.setdefault(str(page_id), []).append(item)

    pages = [
        {
            "page_id": str(pid),
            "title": title,
            "description": description,
            "kind": kind,
            "facts": by_page.get(str(pid), []),
        }
        for pid, title, description, kind in page_rows
    ]
    if orphans:
        pages.append(
            {
                "page_id": None, "title": ORPHAN_TITLE, "description": "",
                # Facts with no page are facts, not procedures — an orphan
                # bucket claiming to be a skill page would be a claim nobody
                # made.
                "kind": "fact",
                "facts": orphans,
            }
        )
    return pages


_SOURCE_LABEL = {"message": "from chat", "document": "from a doc", "github": "from the repo"}


def annotate(fact: dict) -> str:
    """A fact's text plus its date/provenance annotation — bare, with no
    leading bullet marker, so each caller adds whatever prefix its own
    context needs (a markdown '- ', a consolidation-prompt '[entry_id] ', ...).

    findings §20.3.1: the measured temporal-reasoning gap (80% vs 41%) turns
    on whether facts reach the reader dated. A fact compiled this morning and
    one compiled in May must not look identical — the reader cannot otherwise
    prioritise what to re-verify against live state.
    """
    bits = []
    if fact.get("valid_from") is not None:
        bits.append(f"as of {fact['valid_from']:%Y-%m-%d}")
    label = _SOURCE_LABEL.get(fact.get("source_kind") or "")
    if label:
        bits.append(label)
    suffix = f"  _({', '.join(bits)})_" if bits else ""
    return f"{fact['text']}{suffix}"


def render_team_wiki(conn, team_id: str) -> str:
    """Member-facing markdown projection. Pages with no active facts are
    skipped (they remain in consolidation context via all_active_pages)."""
    sections: list[str] = []
    for p in all_active_pages(conn, team_id):
        if not p["facts"]:
            continue
        skill = p.get("kind") == "skill"
        # §20.3.1's lesson applied to a second column: memory that is stored
        # and then dropped at the render boundary may as well not exist. A
        # skill page rendering identically to a fact page has changed nothing
        # for the agent reading it — "how the team does something" is a
        # different claim from "this is true", and the reader has to be able
        # to tell them apart.
        header = f"## {p['title']}" + (" (procedure)" if skill else "")
        if p["description"]:
            header += f"\n_{p['description']}_"
        # Numbered for a procedure: it has an order, and bullets throw it away.
        marker = (lambda i: f"{i}.") if skill else (lambda _: "-")
        bullets = "\n".join(
            f"{marker(i)} {annotate(f)}" for i, f in enumerate(p["facts"], 1)
        )
        sections.append(f"{header}\n\n{bullets}")
    if not sections:
        return "# Team wiki\n\n_(empty)_"
    return "# Team wiki\n\n" + "\n\n".join(sections)
