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
        "select id, title, description from public.memory_pages"
        " where team_id = %s order by title",
        (team_id,),
    ).fetchall()
    fact_rows = conn.execute(
        "select e.page_id, e.id, v.fact from public.memory_entries e"
        " join public.memory_versions v on v.entry_id = e.id and v.is_active"
        " where e.team_id = %s and not e.archived order by v.created_at",
        (team_id,),
    ).fetchall()

    by_page: dict[str, list[dict]] = {}
    orphans: list[dict] = []
    for page_id, entry_id, fact in fact_rows:
        item = {"entry_id": str(entry_id), "text": fact}
        if page_id is None:
            orphans.append(item)
        else:
            by_page.setdefault(str(page_id), []).append(item)

    pages = [
        {
            "page_id": str(pid),
            "title": title,
            "description": description,
            "facts": by_page.get(str(pid), []),
        }
        for pid, title, description in page_rows
    ]
    if orphans:
        pages.append(
            {"page_id": None, "title": ORPHAN_TITLE, "description": "", "facts": orphans}
        )
    return pages


def render_team_wiki(conn, team_id: str) -> str:
    """Member-facing markdown projection. Pages with no active facts are
    skipped (they remain in consolidation context via all_active_pages)."""
    sections: list[str] = []
    for p in all_active_pages(conn, team_id):
        if not p["facts"]:
            continue
        header = f"## {p['title']}"
        if p["description"]:
            header += f"\n_{p['description']}_"
        bullets = "\n".join(f"- {f['text']}" for f in p["facts"])
        sections.append(f"{header}\n\n{bullets}")
    if not sections:
        return "# Team wiki\n\n_(empty)_"
    return "# Team wiki\n\n" + "\n\n".join(sections)
