"""What each tool is allowed to touch, and whether a human must see it first.

findings §15.4. Three columns, one row per tool:

  surface      which boundary the call crosses — sandbox | db | outbound
  writes       does it change anything (conservative default: True)
  needs_human  must a member approve before it happens (default: True for
               anything outbound)

The load-bearing property is the DEFAULT, not the table: an unregistered tool
resolves to outbound/writes/needs_human, so a tool nobody classified fails
closed. Claude Code's Tool.ts makes the same inversion — isReadOnly defaults
to False, "assume writes" — and it is why forgetting to declare is safe there.

Scope (§15.5): the chokepoint governs `db` and `outbound`. It does NOT govern
a shell inside the sandbox — if the sandbox holds no credentials and cannot
reach anything unproxied, per-command approval buys nothing and costs the
capability the owner refused to trade away.

This is defence in depth ABOVE the database, never a replacement for it. RLS
is still the authorization layer: a tool this table waves through is still
constrained by the policies on the role it runs as.

Keys are MODEL-FACING tool names (what the LLM calls). They are a different
namespace from shared/consent.py's `_EXECUTORS`, which keys on consent action
names — `team_propose_task` proposes the action `task_create`.
"""
from dataclasses import dataclass
from typing import Literal

Surface = Literal["sandbox", "db", "outbound"]


@dataclass(frozen=True)
class ToolSpec:
    surface: Surface
    writes: bool
    needs_human: bool


# The fail-closed default. Anything not in REGISTRY resolves to this.
UNKNOWN = ToolSpec(surface="outbound", writes=True, needs_human=True)

REGISTRY: dict[str, ToolSpec] = {
    # Reads. The agent runs these as the requesting member (findings §4.1), so
    # RLS is already the gate — nothing to add.
    "team_get_state":    ToolSpec("db", writes=False, needs_human=False),
    "memory_read_page":  ToolSpec("db", writes=False, needs_human=False),
    "messages_search":   ToolSpec("db", writes=False, needs_human=False),
    "document_read":     ToolSpec("db", writes=False, needs_human=False),
    "task_get":          ToolSpec("db", writes=False, needs_human=False),
    # Touches no database at all, but "db" is the honest surface for "reads
    # server state" — inventing a fourth surface for one clock tool buys
    # nothing.
    "now":               ToolSpec("db", writes=False, needs_human=False),
    # Proposes into the consent queue. The write it describes is gated by the
    # queue itself, so the TOOL call is not the thing a human approves —
    # needs_human here would deadlock the tool whose whole job is to CREATE
    # the approval request.
    "team_propose_task":    ToolSpec("db", writes=True, needs_human=False),
    "task_propose_update":  ToolSpec("db", writes=True, needs_human=False),
    # Sends immediately into another member's private thread, with no consent
    # gate — the agent's one ungated write (findings §9, exception recorded in
    # §13.7). Declared outbound so the asymmetry is visible in the table
    # rather than only in a doc.
    "member_send_nudge": ToolSpec("outbound", writes=True, needs_human=False),
}


def spec_for(tool_name: str) -> ToolSpec:
    """The tool's declaration, or the fail-closed default. Never raises."""
    return REGISTRY.get(tool_name, UNKNOWN)
