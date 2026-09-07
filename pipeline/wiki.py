"""Read-side wiki projection over the compiled memory.

Pages group facts into topics. This module is pure read/projection:
  - all_active_pages(): pages with their active facts — the compiler's
    consolidation context.
  (The recall index is NOT here: agent/agent.py:wiki_section builds the
  titles+descriptions projection inline and injects it into the system prompt
  every turn. page_index() was a duplicate of that and was deleted 2026-08-29.)

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
        "select e.page_id, e.id, v.fact, v.valid_from, v.id, v.trust,"
        "       (select c.source_kind from public.memory_citations c"
        "         where c.version_id = v.id order by c.created_at limit 1)"
        " from public.memory_entries e"
        " join public.memory_versions v on v.entry_id = e.id and v.is_active"
        " where e.team_id = %s and not e.archived order by v.created_at",
        (team_id,),
    ).fetchall()

    by_page: dict[str, list[dict]] = {}
    orphans: list[dict] = []
    for page_id, entry_id, fact, valid_from, version_id, trust, source_kind in fact_rows:
        item = {
            "entry_id": str(entry_id),
            "text": fact,
            "valid_from": valid_from,
            # The version this snapshot READ. A compile that decides to revise
            # this entry binds its update to this id, so a second compile
            # cannot erase a first one it never saw.
            "version_id": str(version_id),
            "trust": trust,
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
    """Return a fact with its date and provenance for consolidation context."""
    bits = []
    if fact.get("valid_from") is not None:
        bits.append(f"as of {fact['valid_from']:%Y-%m-%d}")
    label = _SOURCE_LABEL.get(fact.get("source_kind") or "")
    if label:
        bits.append(label)
    suffix = f"  _({', '.join(bits)})_" if bits else ""
    return f"{fact['text']}{suffix}"


# ---------------------------------------------------------------------------
# The index (T20)
# ---------------------------------------------------------------------------

#: How many pages the always-in-prompt index may list.
#:
#: 🔴 There was no cap, and no need for one while the index was built by
#: loading the whole wiki — the cost was already unbounded in a worse way. The
#: index is paid on EVERY turn of EVERY thread, so it is the one part of the
#: prompt that must not grow with the corpus.
MAX_INDEX_PAGES = 120


def page_index(conn, team_id: str) -> list[dict]:
    """Page titles and descriptions, without reading a single fact.

    🔴 `wiki_section` renders titles and descriptions ONLY, and built them by
    calling `all_active_pages` — which loads every active fact of every page,
    with its `valid_from` and its first citation kind. On every turn. A team
    with two thousand facts paid for two thousand rows to print a list of page
    names, and that cost grew with the wiki forever.

    `exists` rather than a join: the question is "does this page have anything
    on it", and a join would fetch the facts to answer it — which is the bug.
    """
    rows = conn.execute(
        "select p.id, p.title, p.description, p.kind"
        " from public.memory_pages p"
        " where p.team_id = %s and exists ("
        "   select 1 from public.memory_entries e"
        "   join public.memory_versions v on v.entry_id = e.id and v.is_active"
        "   where e.page_id = p.id and not e.archived"
        " ) order by p.title limit %s",
        (team_id, MAX_INDEX_PAGES + 1),
    ).fetchall()
    pages = [
        {"page_id": str(r[0]), "title": r[1], "description": r[2], "kind": r[3]}
        for r in rows[:MAX_INDEX_PAGES]
    ]
    # 🔴 Nearly dropped. Entries that predate pages have no page_id, and a
    # metadata-only index over `memory_pages` cannot see them — so those facts
    # would have vanished from the index entirely, which is the starvation
    # this system is most afraid of. `all_active_pages` surfaces them under a
    # virtual bucket and so must this. Caught by an existing test.
    if _has_orphans(conn, team_id):
        pages.append({
            "page_id": None, "title": ORPHAN_TITLE, "description": "",
            "kind": "fact",
        })
    return pages


def _has_orphans(conn, team_id: str) -> bool:
    return conn.execute(
        "select exists (select 1 from public.memory_entries e"
        " join public.memory_versions v on v.entry_id = e.id and v.is_active"
        " where e.team_id = %s and e.page_id is null and not e.archived)",
        (team_id,),
    ).fetchone()[0]


def index_is_truncated(conn, team_id: str) -> bool:
    """Whether the wiki has more pages than the index shows.

    Asked separately so the renderer can SAY so. Silently showing part of the
    wiki teaches the agent the rest does not exist, and it will pass that on to
    the team with confidence.
    """
    row = conn.execute(
        "select count(*) from public.memory_pages p"
        " where p.team_id = %s and exists ("
        "   select 1 from public.memory_entries e"
        "   join public.memory_versions v on v.entry_id = e.id and v.is_active"
        "   where e.page_id = p.id and not e.archived"
        " )",
        (team_id,),
    ).fetchone()
    return bool(row and row[0] > MAX_INDEX_PAGES)


def page_facts(conn, team_id: str, title: str, limit: int) -> dict | None:
    """One page's active facts, read by title, without touching other pages.

    🔴 `read_memory_page` loaded every page's every fact and then picked one
    out of the list in Python.
    """
    wanted = title.strip()
    orphan = wanted.lower() == ORPHAN_TITLE.lower()
    page = None
    if not orphan:
        page = conn.execute(
            "select id, title, description, kind from public.memory_pages"
            " where team_id = %s and lower(title) = lower(%s)",
            (team_id, wanted),
        ).fetchone()
        if page is None:
            return None
    rows = conn.execute(
        "select e.id, v.fact, v.valid_from, v.id"
        " from public.memory_entries e"
        " join public.memory_versions v on v.entry_id = e.id and v.is_active"
        " where e.team_id = %s and not e.archived"
        "   and (%s::uuid is null and e.page_id is null"
        "        or e.page_id = %s::uuid)"
        " order by v.created_at limit %s",
        (team_id, None if orphan else page[0], None if orphan else page[0],
         limit + 1),
    ).fetchall()
    facts = [
        {"entry_id": str(r[0]), "text": r[1], "valid_from": r[2],
         "version_id": str(r[3])}
        for r in rows[:limit]
    ]
    if orphan:
        return {
            "page_id": None, "title": ORPHAN_TITLE, "description": "",
            "kind": "fact", "facts": facts, "truncated": len(rows) > limit,
        }
    return {
        "page_id": str(page[0]), "title": page[1], "description": page[2],
        "kind": page[3], "facts": facts, "truncated": len(rows) > limit,
    }
