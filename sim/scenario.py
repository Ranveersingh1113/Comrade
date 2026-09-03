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
from shared.db import user_session  # noqa: E402

API = "http://localhost:8000"
STATE = json.loads((Path(__file__).resolve().parent / "state.json").read_text())
TEAM = STATE["team_id"]
WHO = {p["tag"]: p for p in STATE["people"]}


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
        return {}
    body = resp.json()
    reply = (body.get("reply") or "").strip()
    print(f"  {'Comrade':14} {reply[:300]}")
    print(f"  {'':14} ({took:.0f}s, run {str(body.get('run_id'))[:8]})")
    TRANSCRIPT.append({"kind": "agent", "asked_by": person["name"],
                       "question": text, "reply": reply,
                       "seconds": round(took, 1),
                       "run_id": body.get("run_id")})
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


def _reset_room() -> None:
    """Clear the room so a re-run measures one conversation, not three.

    Admin, and only here: a scenario that appended to a previous run's chat
    would have the wiki compiling the same decisions repeatedly and the agent
    answering from a history no real team would have.
    """
    import psycopg

    from shared.config import settings

    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    for table in ("memory_citations", "memory_versions", "memory_pages",
                  "consent_queue", "tasks", "agent_steps", "agent_runs",
                  "messages", "github_repos", "github_installations", "jobs"):
        try:
            conn.execute(f"delete from public.{table} where team_id = %s",
                         (TEAM,))
        except Exception:  # noqa: BLE001 - a few have no team_id; skip those
            pass
    conn.close()


def main() -> None:
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


if __name__ == "__main__":
    main()
