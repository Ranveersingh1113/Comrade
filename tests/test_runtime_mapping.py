"""Pure event->step mapping for the agent runtime (no LLM, no DB)."""
from types import SimpleNamespace

from agent.runtime import _reply_from_steps, _steps_from_event


def _event(*parts):
    return SimpleNamespace(content=SimpleNamespace(parts=list(parts)))


def _call_part(name, args):
    return SimpleNamespace(
        function_call=SimpleNamespace(name=name, args=args),
        function_response=None, text=None,
    )


def _result_part(name, response):
    return SimpleNamespace(
        function_call=None,
        function_response=SimpleNamespace(name=name, response=response),
        text=None,
    )


def _text_part(text):
    return SimpleNamespace(function_call=None, function_response=None, text=text)


def test_maps_tool_call_part():
    steps = _steps_from_event(_event(_call_part("team_get_state", {})), 0)
    assert steps == [
        {"seq": 0, "type": "tool_call", "tool": "team_get_state", "args": {}}
    ]


def test_maps_result_and_text_with_continuing_seq():
    ev = _event(
        _result_part("team_get_state", {"members": []}),
        _text_part("All caught up."),
    )
    steps = _steps_from_event(ev, 3)
    assert [s["seq"] for s in steps] == [3, 4]
    assert steps[0]["type"] == "tool_result"
    assert steps[1] == {"seq": 4, "type": "text", "text": "All caught up."}


def test_empty_event_yields_nothing():
    assert _steps_from_event(SimpleNamespace(content=None), 0) == []


def test_reply_joins_text_steps_only():
    steps = [
        {"seq": 0, "type": "tool_call", "tool": "x", "args": {}},
        {"seq": 1, "type": "text", "text": "Hello "},
        {"seq": 2, "type": "text", "text": "world."},
    ]
    assert _reply_from_steps(steps) == "Hello world."
