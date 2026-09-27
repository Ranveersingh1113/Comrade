"""Four people build a snake game, and Comrade is in the room.

Plain chat is inserted as each member under their own RLS. Anything addressed
to Comrade is admitted and then FOLLOWED through the durable run stream with
that member's token, which is exactly what the browser does — so the agent sees
the same history, the same tools and the same permissions it would in the
product, and this sees the same answer the member would.

The chat is written to contain DECISIONS, because the wiki compiler's whole job
is to notice them. Whether it does is part of what this measures.
"""
import json
import sys
import time
import uuid
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


#: Resolved once per run, per person. 🔴 (fix.md F64) `thread_type` is the
#: PRE-THREAD-ROWS contract and no longer exists anywhere: `TurnRequest`
#: requires a `thread_id`, and `messages.thread_type` was dropped when
#: `messages.thread_id` became not-null. This scenario was speaking to an API
#: and a schema that had both moved on, so its first chat line raised and its
#: first ask would have been rejected 422.
_THREADS: dict[str, str] = {}


def _thread_for(person: dict, thread: str = "group") -> str:
    """The real thread row this line belongs in.

    "group" is the team's General thread, which a trigger creates with the team
    (`uq_threads_general` — one per team, so it is SELECTED, never inserted).
    "private" is a restricted thread owned by the asker, created the way the
    product's own policy allows: `au_threads_insert` accepts a thread whose
    `created_by` is the member, and `au_thread_participants_insert` accepts the
    creator adding themselves. Resolved under that member's own session, so a
    thread they could not legitimately reach is not one this can use.
    """
    key = "group" if thread == "group" else f"private:{person['id']}"
    if key in _THREADS:
        return _THREADS[key]
    with user_session(person["id"]) as conn:
        if thread == "group":
            row = conn.execute(
                "select id from public.threads where team_id=%s"
                " and visibility='team' and kind='discussion' and title='General'",
                (TEAM,),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "the team has no General thread; run sim/setup.py first"
                )
            resolved = str(row[0])
        else:
            title = f"Private — {person['name']}"
            row = conn.execute(
                "select id from public.threads where team_id=%s"
                " and visibility='restricted' and created_by=%s and title=%s",
                (TEAM, person["id"], title),
            ).fetchone()
            if row is None:
                # 🔴 THE ID IS GENERATED HERE, and there is no `returning id`.
                # Found by running this against the real database rather than
                # a double: `au_threads_select` is `can_access_thread`, which
                # for a restricted thread means a `thread_participants` row —
                # and the creator has none yet at the moment of the INSERT. So
                # `returning id` fails the SELECT policy on the row it just
                # wrote ("new row violates row-level security policy"), and
                # there is no way to look the id up afterwards either.
                #
                # Supplying the id breaks the deadlock without weakening
                # anything: the participant row is what `is_thread_creator`
                # authorises, and that function is security definer, so the
                # creator can add themselves to a thread they cannot yet read.
                resolved = str(uuid.uuid4())
                conn.execute(
                    "insert into public.threads"
                    " (id, team_id, title, visibility, kind, created_by)"
                    " values (%s,%s,%s,'restricted','discussion',%s)",
                    (resolved, TEAM, title, person["id"]),
                )
                conn.execute(
                    "insert into public.thread_participants"
                    " (thread_id, team_id, user_id, added_by)"
                    " values (%s,%s,%s,%s) on conflict do nothing",
                    (resolved, TEAM, person["id"], person["id"]),
                )
            else:
                resolved = str(row[0])
    _THREADS[key] = resolved
    return resolved


def say(tag: str, text: str) -> None:
    """Plain chat, inserted as that member."""
    person = WHO[tag]
    thread_id = _thread_for(person, "group")
    with user_session(person["id"]) as conn:
        conn.execute(
            "insert into public.messages (team_id, thread_id, sender_kind,"
            " sender_id, body) values (%s,%s,'user',%s,%s)",
            (TEAM, thread_id, person["id"], text),
        )
    print(f"  {person['name']:14} {text}")
    TRANSCRIPT.append({"kind": "chat", "who": person["name"], "text": text})


#: One turn's whole life, approvals included. These turns clone repositories,
#: run tests and open pull requests, so this is generous on purpose — but it is
#: a bound, because a scenario that hangs is a scenario nobody runs.
TURN_TIMEOUT_SECONDS = 900
#: How many permission waits one turn may pass through. "Create tasks for the
#: three work items" parks once per task, so three is the real number and the
#: fourth is the margin; beyond that it is a loop, not a conversation.
MAX_APPROVALS_PER_TURN = 4
#: The run is over and will produce nothing more (server/app.py).
TERMINAL_STATUSES = {"done", "failed", "cancelled"}
WAITING_STATUSES = {"waiting_for_permission", "waiting_for_user"}


def _headers(person: dict) -> dict:
    return {"Authorization": f"Bearer {person['token']}",
            "Content-Type": "application/json"}


def _note_failure(person: dict, text: str, detail: str) -> dict:
    """Record an HTTP-level failure the way score_team_scenario reads it."""
    print(f"  {'COMRADE':14} {detail[:160]}")
    TRANSCRIPT.append({"kind": "error", "who": person["name"],
                       "text": text, "detail": detail[:300]})
    if CHECK_MODE:
        raise RuntimeError(f"agent turn failed for {person['name']}: {detail[:200]}")
    return {}


def _remaining(deadline: float) -> float:
    """What is left of this turn's budget, on a monotonic clock."""
    return deadline - time.monotonic()


def _stream_segment(person: dict, state: dict, deadline: float,
                    method: str, url: str, **kwargs) -> str | None:
    """One stream of run frames, bounded by what is LEFT of the turn's budget.

    Returns a description of an HTTP or transport failure, or None — a turn
    that ran out of time is not a failure of this call, it is recorded on the
    state and handled once, at the end of the turn.
    """
    remaining = _remaining(deadline)
    if remaining <= 0:
        state["timed_out"] = True
        return None
    try:
        with httpx.stream(
            method, url, headers=_headers(person),
            timeout=httpx.Timeout(remaining, connect=min(30.0, remaining)),
            **kwargs,
        ) as response:
            if response.status_code != 200:
                response.read()
                return f"[{response.status_code}] {response.text}"
            _consume(response, state, deadline)
    except httpx.TimeoutException:
        # The OTHER shape: a stream that goes completely quiet. The in-loop
        # check in _consume is for the noisy one, where heartbeats keep a read
        # timeout from ever firing.
        state["timed_out"] = True
    except httpx.HTTPError as exc:
        return f"stream failed: {exc}"
    return None


def _abort_turn(person: dict, text: str, state: dict, reason: str) -> dict:
    """Give up on a turn — and do not leave its run executing.

    🔴 CANCELLED, not abandoned, whatever the reason for giving up. When a turn
    is abandoned its run is very often still EXECUTING, and a scenario that
    walks away leaves a worker writing into the team that the next ask is about
    to read. Stopping the gate's processes afterwards is not a substitute: the
    run is durable, and lease recovery hands it to whichever worker comes next.
    The scenario owns this run, and only the member who asked may stop it —
    which is the token used here.

    🔴 EVERY abort comes through here (fix.md F64, third pass). It used to be
    reachable only from the timeout path, so an approval POST that raised
    unwound straight out of `ask` — no cancellation, and no failure evidence
    for the scorer either, because `http_errors` is written by `_note_failure`.

    A cancellation that itself fails is kept in the reason rather than
    swallowed: "we could not stop it" is the part an operator needs.
    """
    outcome = "no run to cancel"
    if state["run_id"]:
        try:
            stopped = httpx.post(
                f"{API}/agent/runs/{state['run_id']}/cancel",
                headers=_headers(person), json={"team_id": TEAM}, timeout=30,
            )
            outcome = f"cancel returned HTTP {stopped.status_code}"
        except httpx.HTTPError as exc:
            outcome = f"cancel failed: {exc}"
    return _note_failure(person, text, f"{reason}; {outcome}")


def _consume(response, state: dict, deadline: float) -> None:
    """Fold one NDJSON stream of durable frames into the turn's state.

    The frame protocol is the browser's: `run`/`status` carry the lifecycle,
    step frames carry a `seq` and the text, and `done` closes a terminal run.
    A `tool_result` whose response holds a `consent_id` is the step the runtime
    parks on — the same thing the room renders as a consent card.

    🔴 THE CLOCK IS CHECKED HERE (fix.md F64, second pass), before each frame
    is folded in, and that placement is the whole fix. `httpx.Timeout(900)` is
    an INACTIVITY timeout on reads, not a 900-second cap on a streamed
    response — and `_run_frames` emits a heartbeat every 15 seconds of silence,
    which resets it forever. A queued or stuck run could therefore hold this
    open indefinitely while the deadline sat unconsulted between segments.
    Checking after folding would be no better: a `done` arriving past the
    budget would still be recorded as a completed answer, which is exactly what
    the review reproduced (`done elapsed 0.062 limit 0.01`).
    """
    for line in response.iter_lines():
        if _remaining(deadline) <= 0:
            state["timed_out"] = True
            return
        line = line.strip()
        if not line:
            continue
        frame = json.loads(line)
        kind = frame.get("type")
        if frame.get("seq") is not None:
            state["last_seq"] = max(state["last_seq"], frame["seq"])
        if kind in ("run", "status", "done"):
            state["status"] = frame.get("status") or state["status"]
            state["run_id"] = frame.get("run_id") or state["run_id"]
            if frame.get("detail"):
                state["detail"] = frame["detail"]
        elif kind == "text":
            state["reply_parts"].append(frame.get("text") or "")
        elif kind == "error":
            state["status"] = "failed"
            state["detail"] = frame.get("detail") or state["detail"]
        elif kind in ("empty", "busy"):
            if frame.get("detail"):
                state["detail"] = frame["detail"]
        response_body = frame.get("response")
        if isinstance(response_body, dict) and response_body.get("consent_id"):
            state["consent_id"] = str(response_body["consent_id"])


def ask(tag: str, text: str, thread: str = "group") -> dict:
    """Address Comrade and FOLLOW THE RUN, the way the room does.

    🔴 (fix.md F64) This used to POST /agent/turn and read `reply` and
    `user_message_id` straight off the response. That endpoint only ADMITS a
    turn — `TurnResponse` is `run_id` and `status`, and agent.worker executes
    it afterwards — so every answer the scenario recorded was the empty string
    and its evidence could not tell a finished turn from a queued one.

    It waits properly now, and it APPROVES what the turn parks on: a scenario
    that needs three tasks created and a pull request proposed cannot get them
    by admitting a turn and walking away. That makes this the post-approval
    continuation path as well as the answer path.
    """
    person = WHO[tag]
    thread_id = _thread_for(person, thread)
    print(f"\n  {person['name']:14} → Comrade: {text}")
    started = time.monotonic()
    deadline = started + TURN_TIMEOUT_SECONDS
    state = {"run_id": None, "status": None, "detail": None,
             "reply_parts": [], "last_seq": -1, "consent_id": None,
             "timed_out": False}

    problem = _stream_segment(
        person, state, deadline, "POST", f"{API}/agent/turn/stream",
        json={"team_id": TEAM, "text": text, "thread_id": thread_id},
    )
    if problem:
        # Through the abort, not straight to the note: by the time a REATTACH
        # fails the run id is known, and a stream that dies mid-admission may
        # still have carried its opening `run` frame.
        return _abort_turn(person, text, state, problem)

    approvals = 0
    while (not state["timed_out"]
           and state["status"] == "waiting_for_permission"
           and approvals < MAX_APPROVALS_PER_TURN):
        consent_id = state["consent_id"]
        if consent_id is None:
            state["detail"] = "parked for permission with no consent card in the run"
            break
        # 🔴 The REMAINING budget, not a fresh one. Each resumed segment used
        # to get the whole 900 seconds again, so a turn with four approvals
        # could legitimately run for an hour under a limit that says fifteen
        # minutes.
        remaining = _remaining(deadline)
        if remaining <= 0:
            state["timed_out"] = True
            break
        try:
            approved = httpx.post(
                f"{API}/consent/{consent_id}/approve", headers=_headers(person),
                json={"team_id": TEAM}, timeout=min(60.0, remaining),
            )
        except httpx.TimeoutException:
            # 🔴 THE ANSWER IS LOST, NOT THE REQUEST. `approve_consent`
            # requeues the run inside the same call, so a response that times
            # out may well be one the server already acted on — the card
            # approved and the turn running again. Retrying the POST would be
            # blindly repeating a side effect; what this does instead is record
            # the uncertainty and stop the run it knows about.
            return _abort_turn(
                person, text, state,
                f"approving {consent_id} did not answer within"
                f" {min(60.0, remaining):.0f}s, so it is unknown whether the"
                " card was approved and the run requeued")
        except httpx.HTTPError as exc:
            return _abort_turn(
                person, text, state,
                f"approving {consent_id} failed in transport: {exc}")
        if approved.status_code != 200:
            return _abort_turn(
                person, text, state,
                f"approving {consent_id} failed:"
                f" [{approved.status_code}] {approved.text}")
        approvals += 1
        state["consent_id"] = None
        print(f"  {'':14} [approved {consent_id[:8]}, resuming]")
        # Reattaching IS attaching — the same endpoint the browser reopens a
        # run with — and `after_seq` is what stops the resumed segment from
        # replaying the steps already folded in above.
        problem = _stream_segment(
            person, state, deadline, "GET",
            f"{API}/agent/runs/{state['run_id']}/stream",
            params={"team_id": TEAM, "after_seq": state["last_seq"]},
        )
        if problem:
            return _abort_turn(person, text, state, problem)

    took = time.monotonic() - started
    if state["timed_out"]:
        return _abort_turn(
            person, text, state,
            f"the turn exceeded {TURN_TIMEOUT_SECONDS}s (last status"
            f" {state['status']!r} after {took:.0f}s)")
    reply = "".join(state["reply_parts"]).strip()
    status = state["status"]
    print(f"  {'Comrade':14} {reply[:300]}")
    print(f"  {'':14} ({took:.0f}s, run {str(state['run_id'])[:8]},"
          f" {status}{f', {approvals} approval(s)' if approvals else ''})")
    TRANSCRIPT.append({"kind": "agent", "asked_by": person["name"],
                       "question": text, "reply": reply,
                       "seconds": round(took, 1),
                       "run_id": state["run_id"],
                       "status": status,
                       "approvals": approvals,
                       "detail": state["detail"]})
    if CHECK_MODE and status in ("failed", "cancelled"):
        raise RuntimeError(
            f"the turn for {person['name']} ended {status}: {state['detail']}"
        )
    if CHECK_MODE and status not in TERMINAL_STATUSES | WAITING_STATUSES:
        raise RuntimeError(
            f"the turn for {person['name']} never settled (last status"
            f" {status!r}) within {TURN_TIMEOUT_SECONDS}s"
        )
    return {"run_id": state["run_id"], "status": status, "reply": reply}


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
    http_errors = [t for t in TRANSCRIPT if t.get("kind") == "error"]

    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True

    runs: list[dict] = []
    steps: list[dict] = []
    # Every message that was itself the INPUT to an agent turn (say() chat is
    # not; ask() is). score_team_scenario uses this to tell a compiled fact
    # that cites plain room chat from one that cites only what someone asked
    # Comrade to do.
    agent_input_message_ids: list[str] = []
    if run_ids:
        run_rows = conn.execute(
            # 🔴 (fix.md F64) `input_message_id` comes from the RUN. It used to
            # be read off the turn response, which does not carry one — the
            # admission reply is `run_id` and `status` — so this list was
            # always empty and every citation check silently passed.
            "select id, input_tokens, output_tokens, created_at, finished_at,"
            "       status, input_message_id"
            " from public.agent_runs where id = any(%s::uuid[])",
            (run_ids,),
        ).fetchall()
        by_id = {str(r[0]): r for r in run_rows}
        for rid in run_ids:  # TRANSCRIPT order == chronological order
            row = by_id.get(rid)
            if row is None:
                continue
            _id, in_tok, out_tok, created, finished, status, input_message_id = row
            if input_message_id:
                agent_input_message_ids.append(str(input_message_id))
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
    # Discovery runs every five minutes; allow that interval plus clone time.
    for _ in range(84):
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
        print("  [NOT CLONED after 7 minutes]")
        if CHECK_MODE:
            raise RuntimeError("repository did not clone within 7 minutes")

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
