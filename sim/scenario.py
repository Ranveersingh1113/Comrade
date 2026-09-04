"""Four people build a snake game, and Comrade is in the room.

Plain chat is inserted as each member under their own RLS. Anything addressed
to Comrade goes through POST /agent/turn with that member's token, which is
exactly what the browser does — so the agent sees the same history, the same
tools and the same permissions it would in the product.

The chat is written to contain DECISIONS, because the wiki compiler's whole job
is to notice them. Whether it does is part of what this measures.
"""
import json
import sys
import time
from pathlib import Path

import httpx

# Windows consoles default to cp1252, which cannot encode the dashes and
# arrows this scenario prints — and a UnicodeEncodeError in a print statement
# would abort a run whose data had already landed correctly.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evaluation.team_scenario import score_team_scenario  # noqa: E402
from shared.db import user_session  # noqa: E402

API = "http://localhost:8000"
STATE = json.loads((Path(__file__).resolve().parent / "state.json").read_text())
TEAM = STATE["team_id"]
WHO = {p["tag"]: p for p in STATE["people"]}

#: Set from main() before anything runs. False keeps today's behaviour
#: (print and continue, exit 0 no matter what); True makes ask(), the clone
#: wait and _reset_room() raise instead of swallowing, and main() exits
#: non-zero when score_team_scenario says the run failed.
CHECK_MODE = False


def _refresh_tokens() -> None:
    """Sign everyone in again at the start of a run.

    Supabase access tokens last an hour, and state.json holds whatever setup
    minted. A scenario re-run later fails every agent call with "Signature has
    expired" — which is the JWT check working correctly, since 60 seconds of
    leeway is for clock skew and not for an hour-old token.
    """
    from shared.config import settings

    for person in WHO.values():
        resp = httpx.post(
            f"{settings.supabase_url.rstrip('/')}/auth/v1/token?grant_type=password",
            headers={"apikey": settings.supabase_anon_key,
                     "Content-Type": "application/json"},
            json={"email": person["email"], "password": "sim-password-1"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise SystemExit(f"could not sign in {person['name']}: {resp.text[:160]}")
        person["token"] = resp.json()["access_token"]
REPO = "Ranveersingh1113/test"

TRANSCRIPT: list[dict] = []


def say(tag: str, text: str) -> None:
    """Plain chat, inserted as that member."""
    person = WHO[tag]
    with user_session(person["id"]) as conn:
        conn.execute(
            "insert into public.messages (team_id, thread_type, sender_kind,"
            " sender_id, body) values (%s,'group','user',%s,%s)",
            (TEAM, person["id"], text),
        )
    print(f"  {person['name']:14} {text}")
    TRANSCRIPT.append({"kind": "chat", "who": person["name"], "text": text})


def ask(tag: str, text: str, thread: str = "group") -> dict:
    """Address Comrade, through the endpoint the browser uses."""
    person = WHO[tag]
    print(f"\n  {person['name']:14} → Comrade: {text}")
    started = time.time()
    resp = httpx.post(
        f"{API}/agent/turn",
        headers={"Authorization": f"Bearer {person['token']}",
                 "Content-Type": "application/json"},
        json={"team_id": TEAM, "text": text, "thread_type": thread},
        timeout=240,
    )
    took = time.time() - started
    if resp.status_code != 200:
        print(f"  {'COMRADE':14} [{resp.status_code}] {resp.text[:160]}")
        TRANSCRIPT.append({"kind": "error", "who": person["name"],
                           "text": text, "detail": resp.text[:300]})
        if CHECK_MODE:
            raise RuntimeError(
                f"agent turn failed for {person['name']}: "
                f"{resp.status_code} {resp.text[:200]}"
            )
        return {}
    body = resp.json()
    reply = (body.get("reply") or "").strip()
    print(f"  {'Comrade':14} {reply[:300]}")
    print(f"  {'':14} ({took:.0f}s, run {str(body.get('run_id'))[:8]})")
    TRANSCRIPT.append({"kind": "agent", "asked_by": person["name"],
                       "question": text, "reply": reply,
                       "seconds": round(took, 1),
                       "run_id": body.get("run_id"),
                       "user_message_id": body.get("user_message_id")})
    return body


#: The real GitHub App installation on the account that owns REPO. In the
#: product this row is written by the install callback after GitHub confirms
#: the person can administer it; the simulation writes it as the leader, which
#: is the same policy path minus the browser redirect.
INSTALLATION_ID = 158514342


def connect_repo() -> None:
    """The lead connects the repository, as a leader would.

    🔴 A REPOSITORY CANNOT BE CONNECTED WITHOUT AN INSTALLATION, and the
    scenario found that the hard way: au_github_repos_insert requires one
    belonging to the same team. The design working as intended — a repo
    reachable only through a credential the team owns — but it means
    "connect a repo" is two rows, not one, and a database reset takes the
    installation with it.
    """
    lead = WHO["priya"]
    with user_session(lead["id"]) as conn:
        conn.execute(
            "insert into public.github_installations"
            " (team_id, installation_id, account_login) values (%s,%s,%s)"
            " on conflict (installation_id) do nothing",
            (TEAM, INSTALLATION_ID, "Ranveersingh1113"),
        )
        conn.execute(
            "insert into public.github_repos"
            " (team_id, repo_full_name, installation_id) values (%s,%s,%s)"
            " on conflict do nothing",
            (TEAM, REPO, INSTALLATION_ID),
        )
    print(f"\n  [Priya installs the App and connects {REPO}]")


#: Tables cleared per team_id on a reset. memory_citations is deliberately
#: NOT here: it has no team_id column (source_id is polymorphic, validated in
#: app rather than by FK — see the init migration), so a team_id-scoped
#: delete against it always raised and was always swallowed. Its rows go
#: away anyway when the memory_versions row that owns them is deleted below
#: (version_id references memory_versions on delete cascade).
_RESET_TABLES = ("memory_versions", "memory_pages", "consent_queue", "tasks",
                  "agent_steps", "agent_runs", "messages", "github_repos",
                  "github_installations", "jobs")


def _reset_room() -> None:
    """Clear the room so a re-run measures one conversation, not three.

    Admin, and only here: a scenario that appended to a previous run's chat
    would have the wiki compiling the same decisions repeatedly and the agent
    answering from a history no real team would have.

    Under CHECK_MODE a delete that fails is not swallowed: it names the
    table and the error and raises, so a broken cleanup fails the check
    instead of quietly leaving stale rows for the next run to score against.
    Without it the behaviour is what it always was — print nothing, move on.
    """
    import psycopg

    from shared.config import settings

    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    for table in _RESET_TABLES:
        try:
            conn.execute(f"delete from public.{table} where team_id = %s",
                         (TEAM,))
        except Exception as exc:  # noqa: BLE001 - reported below, not silent
            if CHECK_MODE:
                conn.close()
                raise RuntimeError(f"reset cleanup failed on {table}: {exc}") from exc
    conn.close()


def _collect_evidence() -> dict:
    """Read back everything score_team_scenario needs to judge this run.

    Admin only, and only here — reading for assessment, the same pattern
    _reset_room and the tool-routing eval (evaluation/run.py) already use.
    It is also the only option: `authenticated` has no SELECT on agent_runs
    or agent_steps at all (20260830090000_close_agent_runs_leak.sql revoked
    it after a private-thread leak), so a member-scoped read could not do
    this even if it were the right role for it.

    Never includes an access token. Nothing selected below is one — state.json
    and WHO hold those in memory for the run and neither is touched here.
    """
    import psycopg

    from shared.config import settings

    run_ids = [t["run_id"] for t in TRANSCRIPT
               if t.get("kind") == "agent" and t.get("run_id")]
    # Every message that was itself the INPUT to an agent turn (say() chat
    # is not; ask() is — see TurnResponse.user_message_id in server/app.py).
    # score_team_scenario uses this to tell a compiled fact that cites plain
    # room chat from one that cites only what someone asked Comrade to do.
    agent_input_message_ids = [t["user_message_id"] for t in TRANSCRIPT
                                if t.get("kind") == "agent" and t.get("user_message_id")]
    http_errors = [t for t in TRANSCRIPT if t.get("kind") == "error"]

    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True

    runs: list[dict] = []
    steps: list[dict] = []
    if run_ids:
        run_rows = conn.execute(
            "select id, input_tokens, output_tokens, created_at, finished_at,"
            "       status"
            " from public.agent_runs where id = any(%s::uuid[])",
            (run_ids,),
        ).fetchall()
        by_id = {str(r[0]): r for r in run_rows}
        for rid in run_ids:  # TRANSCRIPT order == chronological order
            row = by_id.get(rid)
            if row is None:
                continue
            _id, in_tok, out_tok, created, finished, status = row
            seconds = (finished - created).total_seconds() if finished else 0.0
            # status carries the runtime's own verdict on the turn. A turn
            # that ran tools and then produced no text is written 'failed'
            # here while the API still answers 200 with an empty reply — the
            # only place that failure is visible without reading prose.
            runs.append({"input_tokens": in_tok or 0, "output_tokens": out_tok or 0,
                         "seconds": seconds, "status": status})

        step_rows = conn.execute(
            "select run_id, seq, type, tool, response"
            " from public.agent_steps where run_id = any(%s::uuid[])"
            " order by seq",
            (run_ids,),
        ).fetchall()
        run_order = {rid: i for i, rid in enumerate(run_ids)}
        step_rows.sort(key=lambda r: run_order.get(str(r[0]), len(run_ids)))
        steps = [{"type": type_, "tool": tool, "response": response}
                 for _rid, _seq, type_, tool, response in step_rows]

    consent_rows = conn.execute(
        "select id, tool_name, status from public.consent_queue where team_id = %s",
        (TEAM,),
    ).fetchall()
    task_consents = [{"id": str(cid), "status": status}
                      for cid, tool_name, status in consent_rows
                      if tool_name == "task_create"]
    pr_consents = [{"id": str(cid), "status": status}
                    for cid, tool_name, status in consent_rows
                    if tool_name == "repo_open_pr"]

    fact_rows = conn.execute(
        "select id, fact from public.memory_versions"
        " where team_id = %s and is_active = true",
        (TEAM,),
    ).fetchall()
    version_ids = [str(vid) for vid, _fact in fact_rows]
    citations_by_version: dict[str, list[str]] = {vid: [] for vid in version_ids}
    if version_ids:
        # source_id is polymorphic (message/document/github — see
        # memory_citations' comment in the init migration); score_team_scenario
        # only cares whether it lands in agent_input_message_ids, so any kind
        # of source_id is fine to hand it unfiltered.
        citation_rows = conn.execute(
            "select version_id, source_id from public.memory_citations"
            " where version_id = any(%s::uuid[])",
            (version_ids,),
        ).fetchall()
        for vid, source_id in citation_rows:
            citations_by_version[str(vid)].append(str(source_id))
    memory_facts = [{"fact": fact, "citations": citations_by_version[str(vid)]}
                     for vid, fact in fact_rows]

    cloned_row = conn.execute(
        "select last_cloned_at from public.github_repos"
        " where team_id = %s and repo_full_name = %s", (TEAM, REPO),
    ).fetchone()
    conn.close()

    return {
        "runs": runs,
        "http_errors": http_errors,
        "repo_cloned": bool(cloned_row and cloned_row[0]),
        "task_consents": task_consents,
        "pr_consents": pr_consents,
        "memory_facts": memory_facts,
        "agent_input_message_ids": agent_input_message_ids,
        "steps": steps,
    }


def _write_evidence(evidence: dict, path: Path | None = None) -> Path:
    """Write the evidence artifact next to transcript.json.

    Refuses to write any of WHO's actual access tokens. Nothing built by
    _collect_evidence should ever carry one, but this is cheap insurance
    against the day a field is added that does. Checked against the live
    token VALUES rather than the word "token" — a repo_run's stdout or a
    GitHub API tool result can legitimately contain that word (e.g. a
    Python "invalid syntax" traceback, or a field named token_type), and a
    keyword match on those is a false alarm that would abort a good run.
    """
    if path is None:
        path = Path(__file__).resolve().parent / "evidence.json"
    blob = json.dumps(evidence, indent=2, default=str)
    leaked = [p["name"] for p in WHO.values() if p.get("token") and p["token"] in blob]
    if leaked:
        raise ValueError(
            f"evidence contains an access token for: {leaked}; refusing to write it"
        )
    path.write_text(blob, encoding="utf-8")
    return path


def main() -> None:
    global CHECK_MODE
    CHECK_MODE = "--check" in sys.argv
    _refresh_tokens()
    _reset_room()
    print("=" * 72)
    print("DAY 1 — the team forms and argues about the design")
    print("=" * 72)
    say("priya", "Kickoff: we're building a snake game in Python for the "
                 "showcase on the 20th. Keep it dependency-free so anyone can "
                 "run it.")
    say("marcus", "Agreed on stdlib only. I'd use curses for rendering — it's "
                  "in the standard library and gives us proper keyboard input.")
    say("aisha", "curses doesn't work on Windows without a shim. Tom and I are "
                 "both on Windows. Can we do a simple loop with a text grid "
                 "instead?")
    say("marcus", "Fair. Decision: plain text grid rendered to stdout, no "
                  "curses. Input via a non-blocking read so it stays portable.")
    say("priya", "Recorded. Marcus takes the game loop and collision, Aisha "
                 "takes rendering and the grid, Tom writes the tests. I'll "
                 "review.")
    say("tom", "I'll aim for tests on collision and growth first — those are "
               "where the bugs live.")
    say("aisha", "One more decision: the grid is 20x20 and the snake starts "
                 "length 3 in the middle, moving right.")

    print()
    print("=" * 72)
    print("Comrade is asked what the team decided")
    print("=" * 72)
    ask("tom", "I joined late — what did the team decide about rendering, and "
               "who is doing what?")

    print()
    print("=" * 72)
    print("DAY 2 — the repository")
    print("=" * 72)
    connect_repo()
    print("  [waiting for the worker to clone it]")
    for _ in range(24):
        time.sleep(5)
        with user_session(WHO["priya"]["id"]) as conn:
            cloned = conn.execute(
                "select last_cloned_at from public.github_repos"
                " where team_id=%s and repo_full_name=%s", (TEAM, REPO),
            ).fetchone()[0]
        if cloned:
            print(f"  [cloned at {cloned}]")
            break
    else:
        print("  [NOT CLONED after 2 minutes]")
        if CHECK_MODE:
            raise RuntimeError("repository did not clone within 2 minutes")

    ask("marcus", "What's currently in our repository?")

    print()
    print("=" * 72)
    print("Comrade is asked to do the work")
    print("=" * 72)
    ask("marcus", "Write the snake game per what we agreed — a 20x20 text "
                  "grid, stdlib only, no curses, snake starts length 3 in the "
                  "middle moving right. Put the game logic in snake.py with "
                  "the movement, growth and collision as functions Tom can "
                  "test. Propose it as a pull request.")

    print()
    print("=" * 72)
    print("DAY 3 — tasks and a private nudge")
    print("=" * 72)
    say("priya", "Showcase is a week out. Where are we?")
    ask("priya", "Create tasks for the three work items we agreed, assigned to "
                 "the right people, due before the 20th.")
    ask("aisha", "Privately: I'm behind on rendering because I got pulled onto "
                 "something else. What's the smallest thing I could do that "
                 "unblocks Tom?", thread="private")

    print()
    print("=" * 72)
    print("Comrade is asked to verify its own work")
    print("=" * 72)
    ask("tom", "Do the tests pass on what Comrade wrote?")

    out = Path(__file__).resolve().parent / "transcript.json"
    out.write_text(json.dumps(TRANSCRIPT, indent=2), encoding="utf-8")
    print(f"\n[transcript → {out}]")

    evidence = _collect_evidence()
    evidence_path = _write_evidence(evidence)
    print(f"[evidence → {evidence_path}]")

    result = score_team_scenario(evidence)
    print()
    print("=" * 72)
    print("SCENARIO " + ("PASSED" if result["passed"] else "FAILED"))
    print("=" * 72)
    for failure in result["failures"]:
        print(f"  - {failure}")
    print(f"  metrics: {result['metrics']}")

    if CHECK_MODE and not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
