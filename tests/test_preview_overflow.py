"""What a browser is told when a preview response is too big.

🔴 THE DEFECT (fix.md A04). The overflow branch ended the response CLEANLY.

`_body()` counts bytes and, past the cap, does `return`. Returning from an
async generator is how a body ENDS normally: the ASGI server sends the
terminating chunk and the transfer completes, carrying whatever status the
upstream sent — commonly 200. So the browser receives a successfully completed
resource that happens to be missing its second half: half a JavaScript bundle
served as though it were whole, which fails later, somewhere else, in a way
nobody traces back to a size limit.

The generator's own docstring claimed the opposite — "the connection ends
without a clean close, which a browser reports as a failed load instead of
rendering half a file as though it were whole". That was the intent. `return`
is not how it is spelled.

WHY THIS TEST USES REAL SOCKETS. The bug is not in the counting; the counting
was right. It is in what the ASGI server does when the generator finishes, and
an in-process TestClient does not run that code — it hands back whatever the
generator yielded and calls it a response. Only a real connection can show the
difference between a body that ended and a body that was cut off, so both ends
here are real: a raw HTTP upstream on one socket, uvicorn serving the app on
another.
"""
import asyncio
import contextlib
import socket
import threading
import time

import httpx
import psycopg
import pytest
import uvicorn

from server import previews
from server.app import PREVIEW_MAX_BYTES, app
from shared.config import settings
from tests._seed import A1, TEAM_A

#: Comfortably past the cap, sent in pieces so the overflow is discovered
#: after the headers have gone — which is the case with no clean answer and
#: therefore the whole point.
CHUNK = b"x" * (1024 * 1024)
CHUNKS_PAST_CAP = (PREVIEW_MAX_BYTES // len(CHUNK)) + 3


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Upstream:
    """A development server, as far as the proxy is concerned.

    Raw asyncio rather than a framework: this has to send a chunked body with
    no content-length, and a declared-length body that is honest about being
    enormous, and those are statements about bytes on a socket.
    """

    def __init__(self) -> None:
        self.port = _free_port()
        self._server = None
        self._thread = None
        self._loop = None

    async def _handle(self, reader, writer):
        request = b""
        while b"\r\n\r\n" not in request:
            piece = await reader.read(4096)
            if not piece:
                writer.close()
                return
            request += piece
        path = request.split(b" ")[1] if b" " in request else b"/"

        if path.startswith(b"/chunked-huge"):
            # No content-length: nothing can be refused up front, so the cap
            # is only discovered partway through.
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/javascript\r\n"
                         b"Transfer-Encoding: chunked\r\n\r\n")
            for _ in range(CHUNKS_PAST_CAP):
                writer.write(f"{len(CHUNK):x}\r\n".encode() + CHUNK + b"\r\n")
                with contextlib.suppress(Exception):
                    await writer.drain()
            writer.write(b"0\r\n\r\n")
        elif path.startswith(b"/declared-huge"):
            size = PREVIEW_MAX_BYTES + 5 * len(CHUNK)
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {size}\r\n\r\n"
                         .encode())
            for _ in range(size // len(CHUNK)):
                writer.write(CHUNK)
                with contextlib.suppress(Exception):
                    await writer.drain()
        else:
            body = b"console.log('small and complete')"
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/javascript\r\n"
                         f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        with contextlib.suppress(Exception):
            await writer.drain()
        with contextlib.suppress(Exception):
            writer.close()

    def start(self) -> None:
        ready = threading.Event()

        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def serve():
                self._server = await asyncio.start_server(
                    self._handle, "127.0.0.1", self.port)
                ready.set()
                async with self._server:
                    await self._server.serve_forever()

            with contextlib.suppress(Exception):
                self._loop.run_until_complete(serve())

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert ready.wait(10), "the fake development server never started"

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)


class Proxy:
    """Comrade's own app, under a real ASGI server on a real port."""

    def __init__(self) -> None:
        self.port = _free_port()
        self._server = None
        self._thread = None

    def start(self) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                log_level="critical", access_log=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 20
        while not getattr(self._server, "started", False):
            assert time.monotonic() < deadline, "the app never came up"
            time.sleep(0.05)

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=10)


@pytest.fixture
def preview(seeded, admin, monkeypatch):
    """A live preview grant pointing at the fake development server."""
    monkeypatch.setattr(settings, "comrade_preview_domain", "preview.test")

    upstream = Upstream()
    upstream.start()

    thread_id = str(admin.execute(
        "select id from public.threads where team_id=%s and title='General'",
        (TEAM_A,),
    ).fetchone()[0])
    process_id = str(admin.execute(
        "insert into public.sandbox_processes (team_id, thread_id, command,"
        " port, container_name, container_id, state)"
        " values (%s,%s,'npm run dev',%s,'127.0.0.1',%s,'running')"
        " returning id",
        (TEAM_A, thread_id, upstream.port, "c" * 64),
    ).fetchone()[0])

    proxy = Proxy()
    proxy.start()

    launched = previews.launch(A1, TEAM_A, process_id)
    host = previews.host_for(process_id)
    redeemed = previews.redeem(launched["grant"], host=host)
    cookie = previews.session_cookie(redeemed)

    client = httpx.Client(
        base_url=f"http://127.0.0.1:{proxy.port}",
        headers={"host": host},
        cookies={cookie["key"]: cookie["value"]},
        timeout=60.0,
    )
    try:
        yield client
    finally:
        client.close()
        proxy.stop()
        upstream.stop()


# ---------------------------------------------------------------------------

def test_a_chunked_overflow_is_not_a_completed_resource(preview):
    """🔴 The finding's own case. No content-length, so nothing can be refused
    before the headers go — and the old code answered by ending the body
    normally, handing the browser a truncated file with a 200 on it."""
    with pytest.raises(httpx.HTTPError):
        response = preview.get("/chunked-huge")
        # Reaching here at all means the transfer completed. Read it so the
        # failure message can say what the browser would have believed.
        pytest.fail(
            f"the browser received a complete {response.status_code} of"
            f" {len(response.content):,} bytes — a truncated file presented"
            f" as a whole one"
        )


def test_a_declared_overflow_is_refused_before_any_body(preview):
    """A content-length that is too large can be answered properly, and must
    be: a clean refusal beats an aborted transfer whenever one is available."""
    response = preview.get("/declared-huge")

    assert response.status_code == 502
    assert len(response.content) < PREVIEW_MAX_BYTES


def test_an_ordinary_response_still_arrives_whole(preview):
    response = preview.get("/small.js")

    assert response.status_code == 200
    assert response.content == b"console.log('small and complete')"


def test_a_normal_request_succeeds_after_an_overflow(preview):
    """The upstream connection and the client are closed on the way out. If
    they were not, one oversized response would poison the preview."""
    with contextlib.suppress(httpx.HTTPError):
        preview.get("/chunked-huge")

    response = preview.get("/small.js")

    assert response.status_code == 200
    assert response.content == b"console.log('small and complete')"


def test_the_transfer_stops_at_the_cap_rather_than_running_on(preview):
    """The cap has to BOUND something, or it is only a log line.

    Counted at the client, not with tracemalloc: the app runs in a thread of
    this same process, so a memory measurement here cannot tell the proxy's
    buffering from httpx's own — it would look like evidence and be nothing of
    the kind. What is observable is how many bytes crossed the socket, and
    that is the property the cap is for.
    """
    received = 0
    with contextlib.suppress(httpx.HTTPError):
        with preview.stream("GET", "/chunked-huge") as response:
            for chunk in response.iter_raw():
                received += len(chunk)

    # One chunk of slack: the count is checked after a chunk is added, so the
    # last one crosses before the abort.
    assert received <= PREVIEW_MAX_BYTES + len(CHUNK), (
        f"{received:,} bytes were forwarded past a {PREVIEW_MAX_BYTES:,} cap"
    )
