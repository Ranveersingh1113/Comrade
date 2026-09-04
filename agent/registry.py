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
from dataclasses import dataclass, field
from typing import Literal

from agent.capability import ArgPolicy
from agent.repo_tools import EDIT_POLICY, READ_POLICY, RUN_POLICY

Surface = Literal["sandbox", "db", "outbound"]


@dataclass(frozen=True)
class ToolSpec:
    surface: Surface
    writes: bool
    needs_human: bool
    #: What this tool's ARGUMENTS may name — paths, commands, a write rate.
    #: Only meaningful for `sandbox` tools; a `db` tool's reach is decided by
    #: RLS underneath it, which is a stronger boundary than any glob.
    #: The default permits nothing, so a sandbox tool that forgets to declare
    #: a scope reaches no files rather than all of them (same inversion as
    #: UNKNOWN below).
    args: ArgPolicy = field(default_factory=ArgPolicy)


# The fail-closed default. Anything not in REGISTRY resolves to this.
UNKNOWN = ToolSpec(surface="outbound", writes=True, needs_human=True)

REGISTRY: dict[str, ToolSpec] = {
    # Reads. The agent runs these as the requesting member (findings §4.1), so
    # RLS is already the gate — nothing to add.
    "team_get_state":    ToolSpec("db", writes=False, needs_human=False),
    "memory_read_page":  ToolSpec("db", writes=False, needs_human=False),
    "memory_search":     ToolSpec("db", writes=False, needs_human=False),
    "messages_search":   ToolSpec("db", writes=False, needs_human=False),
    "document_read":     ToolSpec("db", writes=False, needs_human=False),
    "task_get":          ToolSpec("db", writes=False, needs_human=False),
    "member_activity":   ToolSpec("db", writes=False, needs_human=False),
    "repo_activity":     ToolSpec("db", writes=False, needs_human=False),
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
    # The team's checked-out repository (agent/repo_tools.py). Surface is
    # `sandbox` because these touch this machine's filesystem — which is what
    # makes the chokepoint inspect their arguments rather than wave them
    # through. Reads only: nothing here writes, so Phase C's edit tool is a
    # separate declaration with a separate policy.
    #
    # The scope is the whole checkout minus what capability.py denies
    # absolutely (secrets, .git). It is deliberately not narrower: an agent
    # asked "why is this failing" cannot know in advance which directory holds
    # the answer, and a scope that guesses wrong is a tool that cannot do its
    # job. Containment is the workspace boundary, not a guess about layout.
    "repo_read": ToolSpec(
        "sandbox", writes=False, needs_human=False, args=READ_POLICY
    ),
    # Neither takes a PATH. A glob takes a pattern and a grep takes a search
    # string, and resolving either as a path is nonsense — `repo_grep("../old")`
    # is a fine search for a literal string and must not be refused for
    # containing "..". They declare derives_paths instead, which says out loud
    # that they check every path they produce (agent/repo_tools.py:_resolve)
    # and lets the chokepoint tell that apart from a tool whose author simply
    # forgot to declare a scope.
    "repo_glob": ToolSpec(
        "sandbox", writes=False, needs_human=False,
        args=ArgPolicy(allow=("**",), derives_paths=True),
    ),
    "repo_grep": ToolSpec(
        "sandbox", writes=False, needs_human=False,
        args=ArgPolicy(allow=("**",), derives_paths=True),
    ),
    # writes=True, needs_human=False — and those two together are the design.
    #
    # An edit changes a WORKING COPY nobody else can see. It reaches the team
    # only when a member approves the pull request, which is the reviewable
    # action and is gated by the consent queue. Asking for approval per file
    # would put a card in someone's inbox for each step of one change, which is
    # the consent fatigue §5 exists to avoid.
    #
    # writes=True is what arms the per-turn write cap in the chokepoint. That
    # is the bound on this tool: not "may it write", but "how much".
    "repo_edit": ToolSpec(
        "sandbox", writes=True, needs_human=False, args=EDIT_POLICY
    ),
    # Surface `db`, not `sandbox`: this writes a consent row and touches no
    # file. The git work happens later, in the executor, under a separate role,
    # after a human has said yes. needs_human=False for the same reason
    # team_propose_task carries it — the call being made IS the request for
    # approval, and gating it would deadlock the tool whose whole job is to ask.
    "repo_propose_pr": ToolSpec("db", writes=True, needs_human=False),
    # The first tool that EXECUTES the team's code rather than reading it.
    #
    # writes=False, and that is not an oversight. The per-turn write cap counts
    # tool calls that edit files, and a command's writes land inside a
    # container against a checkout that `sync_repo` resets at the start of
    # every turn. Counting them against the same cap as repo_edit would mean a
    # test run that writes a cache file eats the budget for the change the
    # agent is actually there to make.
    #
    # needs_human=False for the same reason repo_edit carries it: what a
    # container with no network can do is bounded by the container, and the
    # reviewable action downstream is still the pull request. A consent card
    # per `pytest -q` is the fatigue §5 exists to avoid, and it would buy
    # nothing a human could meaningfully judge.
    "repo_run": ToolSpec(
        "sandbox", writes=False, needs_human=False, args=RUN_POLICY
    ),
}


def spec_for(tool_name: str) -> ToolSpec:
    """The tool's declaration, or the fail-closed default. Never raises."""
    return REGISTRY.get(tool_name, UNKNOWN)
