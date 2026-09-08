"""How much memory an untrusted preview server can make the API allocate.

🔴 THE DEFECT (fix.md F03). The proxy opened its upstream connection with
`websockets.connect(..., max_size=None)`, which turns off message-size limits
entirely. The upstream side of a preview is a development server written by a
model and running a team's own unreviewed code, on the far side of a socket
the API holds open — and the iterator assembles a complete message in memory
before handing it over. One frame declared large enough exhausts the
internet-facing process, whatever the HTTP body limits elsewhere say.

The cap is checked here against the real `websockets` library rather than
asserted about, because the whole finding is that the library does exactly what
it was told and the thing it was told was wrong.
"""
import asyncio
import re
from pathlib import Path

import pytest
import websockets

from server.app import PREVIEW_WS_MAX_BYTES

ROOT = Path(__file__).resolve().parent.parent


async def _serve(payload: str):
    """A server that sends one frame and waits, like a chatty dev server."""
    async def handler(connection):
        await connection.send(payload)
        await asyncio.sleep(5)

    return await websockets.serve(handler, "127.0.0.1", 0)


def _port(server) -> int:
    return next(iter(server.sockets)).getsockname()[1]


# ---------------------------------------------------------------------------

def test_an_oversized_upstream_message_closes_the_connection():
    """🔴 With `max_size=None` this frame is assembled in full. The bound has
    to be enforced by the reader, not by hoping upstream behaves."""
    async def _go():
        server = await _serve("x" * (PREVIEW_WS_MAX_BYTES + 4096))
        try:
            async with websockets.connect(
                f"ws://127.0.0.1:{_port(server)}/",
                max_size=PREVIEW_WS_MAX_BYTES, open_timeout=5,
            ) as upstream:
                with pytest.raises(websockets.exceptions.WebSocketException):
                    async for _ in upstream:
                        pass
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(asyncio.wait_for(_go(), timeout=30))


def test_a_fragmented_oversized_message_is_bounded_too():
    """Fragmentation is the obvious way around a per-frame check: the limit
    has to apply to the assembled MESSAGE, which is the thing held in memory."""
    async def _go():
        chunk = "y" * 65536
        pieces = (PREVIEW_WS_MAX_BYTES // len(chunk)) + 4

        async def handler(connection):
            await connection.send((chunk for _ in range(pieces)))
            await asyncio.sleep(5)

        server = await websockets.serve(handler, "127.0.0.1", 0)
        try:
            async with websockets.connect(
                f"ws://127.0.0.1:{_port(server)}/",
                max_size=PREVIEW_WS_MAX_BYTES, open_timeout=5,
            ) as upstream:
                with pytest.raises(websockets.exceptions.WebSocketException):
                    async for _ in upstream:
                        pass
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(asyncio.wait_for(_go(), timeout=60))


def test_an_ordinary_hot_reload_message_still_passes():
    """The bound must not break the feature it protects. A Vite HMR update for
    a large module graph is tens of kilobytes, not megabytes."""
    async def _go():
        payload = '{"type":"update","updates":[' + '{"path":"/src/App.tsx"},' * 400 + ']}'
        server = await _serve(payload)
        try:
            async with websockets.connect(
                f"ws://127.0.0.1:{_port(server)}/",
                max_size=PREVIEW_WS_MAX_BYTES, open_timeout=5,
            ) as upstream:
                received = await asyncio.wait_for(anext(aiter(upstream)), timeout=10)
        finally:
            server.close()
            await server.wait_closed()
        return received

    assert asyncio.run(asyncio.wait_for(_go(), timeout=30)).startswith('{"type":"update"')


# ---------------------------------------------------------------------------

def test_the_proxy_does_not_disable_the_limit():
    """🔴 `max_size=None` was passed explicitly, so this is a check that the
    line stays gone rather than a check that a default happens to be safe."""
    source = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
    # Comments stripped: the constant's docstring quotes the old value on
    # purpose, and a check that cannot tell prose from code would forbid
    # naming the defect it exists to prevent.
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )

    assert "max_size=None" not in code, (
        "the preview proxy turns off the upstream message-size limit"
    )
    assert re.search(r"max_size=PREVIEW_WS_MAX_BYTES", code), (
        "the upstream connection does not carry the bound"
    )


def test_the_bound_is_a_sane_size():
    """Large enough for real hot-reload traffic, small enough that a few
    concurrent previews cannot take the process down."""
    assert 256 * 1024 <= PREVIEW_WS_MAX_BYTES <= 16 * 1024 * 1024
