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


#: A real-shaped team id. The gate derives the workspace path from it, and
#: shared/workspace.py refuses anything that is not a UUID — so a placeholder
#: like "team-1" would be rejected before any policy was consulted.
TEAM = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class _Ctx:
    """The slice of ToolContext the gate touches: a mutable state dict.

    Was `None` — honest while the gate read only the tool's declaration. It now
    counts writes per turn AND derives the workspace root from `team_id`, so a
    context carrying neither would make both untestable.
    """

    def __init__(self, state=None):
        self.state = {"team_id": TEAM} if state is None else state


def _gate(tool_name: str, tool_args=None, ctx=None):
    """Run the gate for one tool call. None = allowed through untouched."""
    return asyncio.run(
        ChokepointPlugin().before_tool_callback(
            tool=BaseTool(name=tool_name, description=""),
            tool_args=tool_args or {},
            tool_context=ctx if ctx is not None else _Ctx(),
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


def test_a_sandbox_tool_declaring_no_scope_reaches_nothing(monkeypatch):
    """🔴 This test used to assert the OPPOSITE, and was right to at the time.

    It read: "the chokepoint governs db and outbound, not a shell in a
    sandbox — a sandbox holding no credentials and reaching nothing unproxied
    buys nothing from per-command approval and costs the capability." Sound
    argument, and both premises are false on a developer's machine: the
    environment holds .env (the RLS-bypassing admin role, the JWT signing
    secret, a GitHub PAT) and reaches the whole internet.

    So the branch that returned None for `sandbox` was the shortest path in
    this codebase to a completely ungated shell — one ToolSpec and nothing
    else. It now applies the tool's argument policy, and a tool that declares
    none reaches no files, matching UNKNOWN's inversion: undeclared means
    unreviewed, not allowed.
    """
    monkeypatch.setitem(
        REGISTRY, "sandbox_shell", ToolSpec("sandbox", writes=False, needs_human=False)
    )
    refusal = _gate("sandbox_shell", {"path": "agent/tools.py"})
    assert isinstance(refusal, dict)
    assert refusal["error"] == "refused_by_capability_budget"


def test_a_sandbox_tool_within_its_declared_scope_runs(monkeypatch):
    from agent.capability import ArgPolicy

    monkeypatch.setitem(
        REGISTRY, "read_source",
        ToolSpec("sandbox", writes=False, needs_human=False,
                 args=ArgPolicy(path_arg="path", allow=("agent/**",))),
    )
    assert _gate("read_source", {"path": "agent/tools.py"}) is None


def test_no_sandbox_scope_can_reach_a_secret(monkeypatch):
    """The deny-list is not a policy choice a tool author can opt out of."""
    from agent.capability import ArgPolicy

    monkeypatch.setitem(
        REGISTRY, "read_anything",
        ToolSpec("sandbox", writes=False, needs_human=False,
                 args=ArgPolicy(path_arg="path", allow=("**",))),
    )
    refusal = _gate("read_anything", {"path": ".env"})
    assert isinstance(refusal, dict)
    assert "secret" in refusal["reason"]


def test_writes_are_capped_per_turn(monkeypatch):
    """The RATE dimension. A loop that has decided to rewrite the repository
    should be stopped by arithmetic, not noticed afterwards."""
    from agent.capability import ArgPolicy

    monkeypatch.setitem(
        REGISTRY, "write_test",
        ToolSpec("sandbox", writes=True, needs_human=False,
                 args=ArgPolicy(path_arg="path", allow=("tests/**",),
                                writable=("tests/**",), max_writes_per_turn=2)),
    )
    ctx = _Ctx()
    args = {"path": "tests/test_agent.py"}
    assert _gate("write_test", args, ctx) is None
    assert _gate("write_test", args, ctx) is None
    refusal = _gate("write_test", args, ctx)
    assert isinstance(refusal, dict)
    assert "limit" in refusal["reason"]


def test_the_write_cap_is_per_turn_not_global(monkeypatch):
    """State lives on the ADK session, and runtime.py builds a fresh one per
    turn — so a member asking a second question starts from zero rather than
    inheriting the last turn's exhaustion."""
    from agent.capability import ArgPolicy

    monkeypatch.setitem(
        REGISTRY, "write_test",
        ToolSpec("sandbox", writes=True, needs_human=False,
                 args=ArgPolicy(path_arg="path", allow=("tests/**",),
                                writable=("tests/**",), max_writes_per_turn=1)),
    )
    args = {"path": "tests/test_agent.py"}
    assert _gate("write_test", args, _Ctx()) is None
    assert _gate("write_test", args, _Ctx()) is None
