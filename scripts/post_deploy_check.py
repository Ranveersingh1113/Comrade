"""Does the DEPLOYED system do the things it is for?

Post-deployment acceptance, run on the pilot host against the running stack:

    docker compose -f docker-compose.yml -f docker-compose.prod.yml \
      --profile migrate run --rm --no-deps -T --entrypoint python \
      migrate -m scripts.post_deploy_check

Run through the `migrate` service because that is the one service given
COMRADE_DB_URL_ADMIN — the api and both workers have it blanked on purpose — and
this needs to arrange and remove its own fixtures.

🔴 A DESIGNATED TEST TEAM, created here and removed at the end. It must not
touch the real teams: every id below is fixed and obviously synthetic, every
write names one of them, and the teardown is by id rather than by pattern. A
verification that damages the thing it verifies is not one.

🔴 NOT A BROWSER SIGN-IN. Nothing here types a password or creates an account
through the product's sign-up. Browser sign-in is verified separately by the
journey suite and, on the deployed host, only as far as the page rendering and
the API refusing an unauthenticated call — which is stated rather than implied.
"""
import json
import os
import time
import uuid

import psycopg

TEAM = "dddddddd-0000-4000-8000-00000000dead"
USER = "dddddddd-0000-4000-8000-00000000beef"
EMAIL = "post-deploy-check@comrade.invalid"

TASK_TITLE = "Verify the telemetry exporter"
PAGE_TITLE = "Decisions"
PAGE_FACT = "The deployment mascot is a quokka"
PAGE_ANSWER = "quokka"
DOC_FACT = "pomegranate"

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, bool(ok), detail))
    print(f"  {label:<52} {'PASS' if ok else '***FAIL'}  {detail[:70]}")


def admin():
    conn = psycopg.connect(os.environ["COMRADE_DB_URL_ADMIN"])
    conn.autocommit = True
    return conn


def teardown() -> list[str]:
    """Remove every fixture root. By id, never by pattern.

    🔴 (fix.md F55) RETURNS ITS FAILURES. This used to catch each delete, print
    it, and return None — so a run whose cleanup failed still reported
    POST-DEPLOY OK and exited 0. A cleanup that fails quietly is worse than one
    that never ran, because it leaves rows in a real database and says it did
    not.
    """
    failures: list[str] = []
    with admin() as conn:
        for sql in (
            "delete from public.jobs where team_id=%s",
            "delete from public.documents where team_id=%s",
            "delete from public.memory_versions where team_id=%s",
            "delete from public.memory_entries where team_id=%s",
            "delete from public.memory_pages where team_id=%s",
            "delete from public.agent_steps where run_id in"
            " (select id from public.agent_runs where team_id=%s)",
            "delete from public.permission_grants where team_id=%s",
            "delete from public.consent_queue where team_id=%s",
            "delete from public.agent_runs where team_id=%s",
            "delete from public.tasks where team_id=%s",
            "delete from public.messages where team_id=%s",
            "delete from public.thread_participants where team_id=%s",
            "delete from public.threads where team_id=%s",
            "delete from public.memberships where team_id=%s",
            "delete from public.teams where id=%s",
        ):
            try:
                conn.execute(sql, (TEAM,))
            except Exception as exc:                     # noqa: BLE001
                failures.append(f"{sql.split()[2]}: {str(exc)[:90]}")
        for sql in (
            "delete from public.profiles where id=%s",
            # auth.identities too: a browser sign-in needs one, and the first
            # version of this did not know it existed.
            "delete from auth.identities where user_id=%s",
            "delete from auth.users where id=%s",
        ):
            try:
                conn.execute(sql, (USER,))
            except Exception as exc:                     # noqa: BLE001
                failures.append(f"{sql.split()[2]}: {str(exc)[:90]}")
    return failures


def fixture_rows_left() -> dict:
    """🔴 EVERY root, not just the team (fix.md F55). A leftover profile or auth
    user coexisting with a clean team count is exactly the shape that let a
    failed cleanup report success."""
    with admin() as conn:
        return {
            "team": conn.execute(
                "select count(*) from public.teams where id=%s", (TEAM,)).fetchone()[0],
            "profile": conn.execute(
                "select count(*) from public.profiles where id=%s", (USER,)).fetchone()[0],
            "auth_user": conn.execute(
                "select count(*) from auth.users where id=%s", (USER,)).fetchone()[0],
            "identity": conn.execute(
                "select count(*) from auth.identities where user_id=%s", (USER,)).fetchone()[0],
            "runs": conn.execute(
                "select count(*) from public.agent_runs where team_id=%s", (TEAM,)).fetchone()[0],
            "consent": conn.execute(
                "select count(*) from public.consent_queue where team_id=%s", (TEAM,)).fetchone()[0],
        }


def furnish() -> str:
    with admin() as conn:
        conn.execute(
            "insert into auth.users (instance_id, id, aud, role, email,"
            " encrypted_password, created_at, updated_at) values"
            " ('00000000-0000-0000-0000-000000000000', %s, 'authenticated',"
            " 'authenticated', %s, '', now(), now())", (USER, EMAIL))
        conn.execute(
            "insert into public.profiles (id, display_name) values (%s,'Checker')"
            " on conflict (id) do update set display_name='Checker'", (USER,))
        conn.execute(
            "insert into public.teams (id, name, created_by)"
            " values (%s,'Post-deploy check',%s)", (TEAM, USER))
        conn.execute(
            "insert into public.memberships (team_id, user_id, role, status)"
            " values (%s,%s,'leader','active')", (TEAM, USER))
        # 🔴 SELECTED, not inserted. Creating a team makes its General thread —
        # `uq_threads_general` is one per team — so inserting one here duplicated
        # what the product had already done. Found by running this.
        thread = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM,)).fetchone()[0]
        task = conn.execute(
            "insert into public.tasks (team_id, assignee_id, title, status,"
            " created_by_kind, created_by_id, thread_id)"
            " values (%s,%s,%s,'proposed','user',%s,%s) returning id",
            (TEAM, USER, TASK_TITLE, USER, thread)).fetchone()[0]
        # 🔴 Confirmed AS the assignee. `trg_tasks_confirm_guard` compares
        # auth.uid() to assignee_id, and an admin connection has no auth.uid()
        # at all — "only the assignee may confirm their own task". Being the
        # table owner does not satisfy a check about who is asking, which is the
        # point of the guard. The claim is set the way tests/_seed.as_user does.
        conn.execute("select set_config('request.jwt.claims', %s, false)",
                     (json.dumps({"sub": USER, "role": "authenticated"}),))
        conn.execute(
            "update public.tasks set status='confirmed', confirmed_at=now()"
            " where id=%s", (task,))
        conn.execute("select set_config('request.jwt.claims', '', false)")
        page = conn.execute(
            "insert into public.memory_pages (team_id, title, description, kind)"
            " values (%s,%s,'What the team has settled','fact') returning id",
            (TEAM, PAGE_TITLE)).fetchone()[0]
        entry = conn.execute(
            "insert into public.memory_entries (team_id, page_id) values (%s,%s)"
            " returning id", (TEAM, page)).fetchone()[0]
        conn.execute(
            "insert into public.memory_versions (entry_id, team_id, fact,"
            " change_type, is_active, valid_from)"
            " values (%s,%s,%s,'added',true,now())", (entry, TEAM, PAGE_FACT))
        return str(thread)


def agent_answers(thread_id: str) -> None:
    """A real turn on the deployed stack, against the configured model."""
    from agent.runtime import run_turn_sync

    for label, prompt, fact in (
        ("task lookup", "What tasks are open right now?", TASK_TITLE),
        ("wiki question", "What did we decide the mascot would be?", PAGE_ANSWER),
        ("team context", "Who is on this team?", "Checker"),
    ):
        started = time.monotonic()
        out = run_turn_sync(TEAM, USER, prompt, thread_id=thread_id)
        seconds = time.monotonic() - started
        reply = (out.get("reply") or "").strip()
        grounded = fact.lower() in reply.lower()
        check(f"agent answers: {label}", bool(reply) and grounded,
              f"{seconds:.1f}s  {reply[:60]!r}")


def document_is_ingested() -> None:
    """The real parse_document handler, through the running pipeline worker."""
    doc = str(uuid.uuid4())
    body = (f"The fruit the team chose is a {DOC_FACT}. "
            "This document exists only for the post-deployment check.")
    with admin() as conn:
        conn.execute(
            "insert into public.documents (id, team_id, uploader_id, kind,"
            " filename, storage_path, status)"
            # 🔴 'parsing', not 'pending'. The statuses are parsing, ready and
            # failed — documents_status_check refused my invented one — and
            # 'parsing' is what the enqueue path itself sets.
            " values (%s,%s,%s,'text','post-deploy-check.txt',%s,'parsing')",
            (doc, TEAM, USER, f"{TEAM}/{doc}/post-deploy-check.txt"))
        conn.execute(
            "insert into public.jobs (team_id, job_type, payload, status)"
            " values (%s,'parse_document',%s,'pending')",
            (TEAM, json.dumps({"document_id": doc, "content": body})))

    deadline = time.monotonic() + 180
    status = parsed = None
    while time.monotonic() < deadline:
        time.sleep(5)
        with admin() as conn:
            row = conn.execute(
                "select status, coalesce(parsed_text,''), coalesce(parse_error,'')"
                " from public.documents where id=%s", (doc,)).fetchone()
        if row and row[0] in ("ready", "failed"):
            status, parsed, error = row[0], row[1], row[2]
            break
    else:
        check("document ingestion: the worker picked it up", False,
              "still pending after 180s")
        return

    check("document ingestion: parsed by the running worker",
          status == "ready", f"status={status}")
    check("document ingestion: the text came through",
          DOC_FACT in (parsed or ""), f"{(parsed or '')[:60]!r}")


def main() -> int:
    print("=== a designated test team, created here and removed at the end ===")
    teardown()
    # 🔴 (fix.md F55) The fixture is created INSIDE the try. It used to be
    # built before it, so a furnish() that failed halfway — a guard refusing a
    # write, a column that is not what I thought — left its rows behind with no
    # teardown at all.
    try:
        thread_id = furnish()
        print(f"  team {TEAM}  thread {thread_id}")
        print()
        print("=== an actual agent answer, on the deployed stack ===")
        agent_answers(thread_id)
        print()
        print("=== document ingestion, through the running pipeline worker ===")
        document_is_ingested()
    finally:
        print()
        print("=== teardown ===")
        cleanup_failures = teardown()
        for failure in cleanup_failures:
            print(f"    *** {failure}")
        left = fixture_rows_left()
        remaining = {k: v for k, v in left.items() if v}
        check("every fixture row is gone", not remaining and not cleanup_failures,
              f"left: {remaining or 'none'}")

    failed = [label for label, ok, _ in results if not ok]
    print()
    print("POST-DEPLOY OK" if not failed
          else f"POST-DEPLOY FAILED: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
