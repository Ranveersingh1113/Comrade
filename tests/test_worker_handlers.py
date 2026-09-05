"""Every job type the database permits has a handler in the real worker.

🔴 THE BUG THIS EXISTS FOR, AND WHY EVERY OTHER TEST MISSED IT.

Handlers register as a side effect of importing their module, and
`pipeline.worker.main()` carries a hand-written list of those imports. It was
missing `pipeline.repo_sync`. So the worker started without the `sync_repo`
handler, every sync job failed three times with "no handler registered", and a
team who had just connected a repository watched the setup screen say CLONING…
until they gave up.

Nothing caught it because pytest imports `pipeline.repo_sync` — the tests for
it say so at the top of the file — which registers the handler in the test
process. `tick()` then worked perfectly in every test, on wiring that did not
exist anywhere else.

So this runs in a SUBPROCESS that imports exactly what `main()` imports and
nothing more. A fresh interpreter is the only place the difference between "the
worker registers this" and "the test suite happened to import it" is visible.
"""
import ast
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from shared.config import settings

ROOT = Path(__file__).resolve().parent.parent

#: Job types the schema allows that deliberately have no handler. `embed`
#: belonged to the vector-retrieval path deleted in July 2026; nothing enqueues
#: one. Listed rather than removed from the constraint, because dropping a
#: value a historical row might still hold is a migration with a real failure
#: mode and no benefit.
KNOWN_UNHANDLED = {"embed"}


def _imports_in_main() -> list[str]:
    """The modules `main()` imports, read from the source.

    Parsed rather than executed: importing worker.py and calling main() would
    start the polling loop. This asks what the function WOULD import, which is
    exactly the list under test.
    """
    tree = ast.parse((ROOT / "pipeline" / "worker.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return [
                alias.name
                for sub in ast.walk(node)
                if isinstance(sub, ast.Import)
                for alias in sub.names
            ]
    raise AssertionError("pipeline/worker.py has no main()")


def _handlers_registered_by(modules: list[str]) -> set[str]:
    """What a fresh interpreter registers after importing exactly `modules`."""
    src = (
        "import json\n"
        + "".join(f"import {m}\n" for m in modules)
        + "from pipeline.worker import _HANDLERS\n"
        "print(json.dumps(sorted(_HANDLERS)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", src], cwd=str(ROOT),
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    import json

    return set(json.loads(proc.stdout.strip().splitlines()[-1]))


def _job_types_the_schema_allows() -> set[str]:
    """Read from the check constraint, so adding a job type to the database
    without a handler fails here rather than at 3am."""
    conn = psycopg.connect(settings.comrade_db_url_admin)
    try:
        row = conn.execute(
            "select pg_get_constraintdef(oid) from pg_constraint"
            " where conname = 'jobs_job_type_check'"
        ).fetchone()
    finally:
        conn.close()
    assert row, "jobs_job_type_check is missing"
    # e.g. CHECK ((job_type = ANY (ARRAY['parse_document'::text, ...])))
    # Cast first, THEN quotes: stripping quotes off "'parse_document'::text"
    # only removes the leading one, and leaves a value that matches nothing.
    return {
        part.strip().replace("::text", "").strip().strip("'")
        for part in row[0].split("ARRAY[")[1].split("]")[0].split(",")
    }


def test_the_worker_registers_a_handler_for_every_job_type():
    """🔴 The wiring, checked where the wiring actually lives.

    A missing import here is silent: the job is claimed, fails on a lookup,
    and burns its three attempts. Nothing in the product says why, and the
    feature that depends on it looks slow rather than broken.
    """
    registered = _handlers_registered_by(_imports_in_main())
    allowed = _job_types_the_schema_allows()
    missing = allowed - registered - KNOWN_UNHANDLED
    assert not missing, (
        f"the worker's main() never imports the module registering: {sorted(missing)}."
        " Handlers register on import, so a job of that type fails three times"
        " with 'no handler registered' and the feature behind it silently"
        " never runs."
    )


def test_a_handler_with_no_job_type_is_also_wrong():
    """The other direction. A handler for a type the schema forbids can never
    be reached, and is either a typo or a migration somebody forgot."""
    registered = _handlers_registered_by(_imports_in_main())
    allowed = _job_types_the_schema_allows()
    assert not (registered - allowed), (
        f"handlers registered for job types the database rejects:"
        f" {sorted(registered - allowed)}"
    )


def test_the_entry_point_runs_where_the_handlers_were_registered():
    """🔴 The bug a four-person simulation found and every test missed.

    `python -m pipeline.worker` executes worker.py as the module `__main__`.
    When a handler module then does `from pipeline.worker import register`,
    Python does not find that name imported yet, so it LOADS THE FILE A SECOND
    TIME as `pipeline.worker`. Two module objects, each with its own
    `_HANDLERS`: register() writes to one, and the loop in `__main__` reads the
    other — empty.

    Every job type failed with "no handler registered": parse_document,
    compile_memory, ingest_github, compile_github, sync_repo,
    build_environment. The entire queue, in the documented way of starting it.

    Every existing test calls tick() or run_once() in a process where
    pipeline.worker was imported normally and there is exactly one copy, so
    none of them could see it. This one observes from the LOOP's side: it
    replaces `pipeline.worker.tick` with a spy and runs the module as
    `__main__`. If main() executes in the canonical module the spy fires; if it
    executes in `__main__` it calls `__main__.tick` instead and the spy never
    runs, which is precisely the split.
    """
    src = (
        "import runpy, sys\n"
        "import pipeline.worker as canonical\n"
        "fired = []\n"
        "canonical.tick = lambda: (fired.append(sorted(canonical._HANDLERS)), 0)[1]\n"
        "class Stop:\n"
        "    def __init__(self): self.calls = 0\n"
        "    def is_set(self): self.calls += 1; return self.calls > 1\n"
        "    def wait(self, *a): return True\n"
        "canonical._stopping = Stop()\n"
        "runpy.run_module('pipeline.worker', run_name='__main__')\n"
        "print('FIRED' if fired else 'SPLIT')\n"
        "print(fired[0] if fired else [])\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", src], cwd=str(ROOT),
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    lines = proc.stdout.strip().splitlines()
    assert lines and lines[0] == "FIRED", (
        "the worker's loop ran in a different module object than the one"
        " register() wrote to — every job type would fail with 'no handler"
        " registered'. The __main__ block must import main from"
        " pipeline.worker rather than calling the local one."
        f" Output: {proc.stdout!r} {proc.stderr[-300:]!r}"
    )
    seen = ast.literal_eval(lines[1])
    assert "sync_repo" in seen and "compile_memory" in seen, seen
