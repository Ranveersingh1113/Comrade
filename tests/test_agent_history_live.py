"""Live proof that the agent remembers the previous turn (real Gemini).

Two turns in one thread, run exactly the way server/app.py runs them: persist
the member's message, run, persist the reply. The second question is only
answerable from the first — nothing in the team's state, wiki or tools carries
the answer.
"""
import asyncio

import pytest

from agent.runtime import run_turn
from server.app import _persist_ai_reply, _persist_user_message
from shared.config import settings
from tests._seed import A1, TEAM_A

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not settings.gemini_api_key, reason="no GEMINI_API_KEY configured"
    ),
]

CODENAME = "Falcon Ridge"


def _turn(text: str, thread_type: str = "private") -> str:
    owner = None if thread_type == "group" else A1
    message_id = _persist_user_message(A1, TEAM_A, thread_type, text)
    result = asyncio.run(run_turn(
        TEAM_A, A1, text,
        thread_type=thread_type, exclude_message_id=message_id,
    ))
    if result["reply"]:
        _persist_ai_reply(TEAM_A, thread_type, owner, result["reply"])
    return result["reply"]


def test_the_second_turn_remembers_the_first(seeded):
    first = _turn(
        f"Just noting something down: we are calling this release {CODENAME}."
        " Nothing to do about it."
    )
    assert first
    second = _turn(
        "What name did I just give the release? Reply with the name only."
    )
    assert "falcon" in second.lower(), f"turn 1: {first!r}\nturn 2: {second!r}"
