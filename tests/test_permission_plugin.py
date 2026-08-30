"""The chokepoint: a tool nobody declared is refused before its body runs.

findings §15.4/§15.5, gap G2. Verified against google-adk 2.2.0: returning a
dict from `before_tool_callback` short-circuits the tool and that dict becomes
the function response the model reads (plugin_manager.run_before_tool_callback
-> flows/llm_flows/functions.py steps 1-3). So a refusal arrives as a tool
result the model can react to, not as an exception that kills the turn.
"""
import asyncio

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.base_tool import BaseTool

from agent.permission_plugin import ChokepointPlugin
from agent.registry import REGISTRY, ToolSpec


def _gate(tool_name: str):
    """Run the gate for one tool call. None = allowed through untouched."""
    return asyncio.run(
        ChokepointPlugin().before_tool_callback(
            tool=BaseTool(name=tool_name, description=""),
            tool_args={},
            # The gate reads the tool's declaration and nothing else, so an
            # absent context is honest here rather than a stub standing in.
            tool_context=None,
        )
    )


def test_the_gate_is_actually_wired_into_the_runner():
    """A chokepoint nobody installed is a comment.

    The Runner the runtime builds must carry the plugin; the App is what
    carries it there, which is why the runtime moved off InMemoryRunner.
    """
    from agent.agent import app

    runner = Runner(app=app, session_service=InMemorySessionService())
    assert any(
        isinstance(p, ChokepointPlugin) for p in runner.plugin_manager.plugins
    )


def test_a_declared_read_passes_through_untouched():
    assert _gate("team_get_state") is None
    assert _gate("memory_read_page") is None


def test_the_declared_writers_pass_through():
    """Both already carry a gate, so the chokepoint must not add a second.

    team_propose_task writes only a proposal — gating the tool whose whole job
    is to CREATE the approval request would deadlock. member_send_nudge is the
    deliberate exception recorded in §13.7.
    """
    assert _gate("team_propose_task") is None
    assert _gate("member_send_nudge") is None


def test_an_undeclared_tool_is_refused_and_the_model_is_told_why():
    """G2: unknown must mean "refuse", not "whatever the body does".

    Fails if UNKNOWN's needs_human default is flipped to permissive — which is
    the property being defended, not an incidental one.
    """
    refusal = _gate("exfiltrate_everything")
    assert refusal is not None, "an undeclared tool was allowed to run"
    assert isinstance(refusal, dict), "a non-dict does not short-circuit the tool"
    assert "exfiltrate_everything" in str(refusal)


def test_a_sandbox_tool_is_not_gated(monkeypatch):
    """§15.5: the chokepoint governs db and outbound, not a shell in a sandbox.

    A sandbox holding no credentials and reaching nothing unproxied buys
    nothing from per-command approval and costs the capability. There is no
    sandbox tool yet, so the policy is asserted against a synthetic
    declaration rather than an invented tool — and the declaration asks for a
    human, so this fails if the sandbox exemption is dropped.
    """
    monkeypatch.setitem(
        REGISTRY, "sandbox_shell", ToolSpec("sandbox", writes=True, needs_human=True)
    )
    assert _gate("sandbox_shell") is None
