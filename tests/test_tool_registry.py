"""Every tool declares its surface; an unclassified one fails closed.

findings §15.4. Claude Code's Tool.ts defaults isReadOnly -> false and
isConcurrencySafe -> false: forgetting to declare gets you the DANGEROUS
assumption. That inversion is the whole point — a registry whose default is
permissive is a registry that only protects the tools someone remembered.
"""
from agent.registry import REGISTRY, ToolSpec, spec_for


def test_every_registered_tool_is_declared():
    from agent.agent import root_agent

    for tool in root_agent.tools:
        assert tool.__name__ in REGISTRY, f"{tool.__name__} is not declared"


def test_an_unknown_tool_fails_closed():
    spec = spec_for("some_tool_nobody_classified")
    assert spec.surface == "outbound"
    assert spec.writes is True
    assert spec.needs_human is True


def test_read_only_tools_are_declared_as_such():
    assert spec_for("team_get_state").writes is False
    assert spec_for("memory_read_page").writes is False
    # the two reading tools: RLS is already the gate, so no human in the loop
    for name in ("messages_search", "document_read"):
        assert spec_for(name) == ToolSpec("db", writes=False, needs_human=False)


def test_the_nudge_is_declared_as_an_outbound_write():
    """member_send_nudge acts immediately and reaches another member's thread.

    findings §9 notes the asymmetry: after §13 the agent may not put a word in
    the shared room on its own initiative, yet may still DM a teammate who
    never asked. The registry must at least SAY so.
    """
    spec = spec_for("member_send_nudge")
    assert spec.surface == "outbound"
    assert spec.writes is True
