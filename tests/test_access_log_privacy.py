"""What the API's own access log writes down about a request.

🔴 THE DEFECT (fix.md F31). `shared/observability.setup()` replaced the ROOT
handler, and root is not where uvicorn logs. The `uvicorn` CLI applies its
packaged `LOGGING_CONFIG` before it imports the app, and that config gives
`uvicorn.access` its own handler with `propagate: false` — so access records
never reach the root redactor at all. Every request line, path and query
string went to stdout unfiltered, and stdout is the copy that leaves the
building.

Started here the way the Dockerfile starts it — `uvicorn server.app:app` —
because the whole finding is that the process configures its own logging
before our code gets a say, and nothing that stubs uvicorn can show that.
"""
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

SENTINEL = "PRIVATE-b9c1f2a7"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def api():
    """The real packaged invocation, with its output captured.

    Function-scoped on purpose: reading the captured output means stopping the
    process, so a shared server would leave every test after the first talking
    to a corpse — and asserting against output the fixture's own health check
    happened to leave behind.
    """
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.app:app",
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        cwd=os.getcwd(),
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            if proc.poll() is not None:
                raise RuntimeError("the API exited: " + (proc.stdout.read() or ""))
            try:
                urllib.request.urlopen(f"{base}/health", timeout=1).read()
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.5)
        else:
            raise RuntimeError("the API never became reachable")
        yield base, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def _get(url: str) -> int:
    """Make the request; the STATUS is not what these tests are about.

    /ready answers 200 on a healthy machine and 503 on one with a stopped
    worker, and either way the access log records the line.
    """
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code


def _output(proc) -> str:
    """Everything the process wrote, after asking it to stop."""
    proc.terminate()
    try:
        return proc.stdout.read() or ""
    except ValueError:  # pragma: no cover - already closed
        return ""


# ---------------------------------------------------------------------------

def test_a_query_value_never_reaches_the_access_log(api):
    """🔴 It did, in full. A query string is where values live — a document
    name, a search term, a token somebody pasted into a URL — and the access
    logger never passed through the redactor."""
    base, proc = api

    _get(f"{base}/ready?token={SENTINEL}")

    output = _output(proc)
    assert SENTINEL not in output, (
        "the access log wrote a query value:\n" + output[-2000:]
    )


def test_the_route_and_status_still_get_logged(api):
    """A redaction that removes the diagnosis is worse than the leak. An
    operator has to be able to see which route was hit and how it went."""
    base, proc = api

    _get(f"{base}/health")

    output = _output(proc)
    assert "/health" in output, output[-2000:]
    assert re.search(r"\b(200|503)\b", output), output[-2000:]


def test_the_redactor_is_attached_to_every_handler_uvicorn_made():
    """The structural half: whatever uvicorn configured before we were
    imported has to end up carrying the filter too."""
    import logging

    from uvicorn.config import LOGGING_CONFIG

    logging.config.dictConfig(LOGGING_CONFIG)      # as the CLI does
    from shared.observability import RedactingFormatter, setup

    setup("api")

    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            assert isinstance(handler.formatter, RedactingFormatter), (
                f"{name} has a handler whose output is not redacted"
            )
