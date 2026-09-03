"""One place every tool call passes through before its body runs.

findings §15.4/§15.5, gaps G1/G2/G7. Before this, whether a tool was gated was
per-tool convention: team_propose_task happened to route through
propose_action, member_send_nudge happened to write directly, and a sixth tool
that forgot to route through either was ungated with nothing to catch it.

Verified against google-adk 2.2.0: PluginManager.run_before_tool_callback runs
first, and flows/llm_flows/functions.py calls the tool only `if
function_response is None`. So returning a dict here short-circuits the tool
and that dict becomes the function response the model reads — a refusal
arrives as a tool result the model can react to, not as an exception that
kills the turn. It also lands in agent_runs as an ordinary tool_result step.

This is defence in depth ABOVE the database. RLS remains the authorization
layer; nothing here decides who may see or write a row.
"""
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools import ToolContext
from google.adk.tools.base_tool import BaseTool

from agent.capability import CapabilityError, check_command, check_path
from agent.registry import spec_for
from shared.workspace import WorkspaceError, workspace_for


class ChokepointPlugin(BasePlugin):
    """Refuses any call that asks for a human gate this runtime cannot offer."""

    def __init__(self) -> None:
        super().__init__(name="chokepoint")

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Optional[dict]:
        """None lets the call run; a dict refuses it and answers the model."""
        spec = spec_for(tool.name)
        if spec.surface == "sandbox":
            # This branch USED to be `return None` — an unconditional allow —
            # on the §15.5 reasoning that "a sandbox with no credentials and no
            # unproxied network gains nothing from per-command consent".
            #
            # That argument is sound and both of its premises are false on a
            # developer's machine. The environment holds .env (the RLS-bypassing
            # admin role, the JWT signing secret, a GitHub PAT) and reaches the
            # whole internet. So a shell here is not a sandbox, and the branch
            # that waved it through was the shortest path in the codebase to an
            # ungated shell — `ToolSpec("sandbox", ...)` and nothing else.
            #
            # Until real containment exists underneath, THIS is the boundary.
            return self._check_arguments(tool.name, spec, tool_args, tool_context)
        if spec.needs_human:
            # Nothing can ask a member mid-turn — approval lives in the consent
            # queue, which a tool must propose INTO rather than wait on. So the
            # honest answer to needs_human is to refuse and say why. An
            # undeclared tool lands here via registry.UNKNOWN: unknown means
            # refuse, not "whatever the body does" (G2).
            return {
                "error": "refused_by_chokepoint",
                "tool": tool.name,
                "reason": (
                    f"{tool.name} needs a member's approval before it can run,"
                    " and this turn has no way to ask for one, so the call was"
                    " refused before its body ran. A tool that is not declared"
                    " in agent/registry.py defaults to needing approval —"
                    " undeclared means unreviewed, not allowed. Use a declared"
                    " tool, or propose the action into the consent queue so a"
                    " member can approve it."
                ),
            }
        return None

    @staticmethod
    def _check_arguments(
        name: str, spec: Any, tool_args: dict[str, Any], ctx: ToolContext
    ) -> Optional[dict]:
        """Path and command scope for a tool that touches this machine.

        Refuses the same way everything else here does — a dict the model
        reads as a tool result — so a blocked path is something it can react
        to and explain, not an exception that kills the turn.
        """
        policy = spec.args
        try:
            if not policy.path_arg and not policy.command_arg and not policy.derives_paths:
                # 🔴 Caught by its own test. Without this, a sandbox tool that
                # named no inspectable argument fell through every branch below
                # and was allowed — the exact fail-OPEN this layer exists to
                # remove, reintroduced by omission rather than by argument.
                #
                # A tool that touches this machine must say which argument
                # carries the path or the command, because that is the only
                # thing that can be scoped. Taking neither is not "harmless",
                # it is "unscopable", and unscopable means refused — the same
                # inversion registry.UNKNOWN makes for an undeclared tool.
                raise CapabilityError(
                    f"{name} runs on this machine but declares no path_arg,"
                    " command_arg or derives_paths, so nothing about it can be"
                    " scoped. Name the argument that carries the path or the"
                    " command in agent/registry.py — or set derives_paths if"
                    " the tool checks the paths it produces itself."
                )
            if policy.path_arg and policy.path_arg in tool_args:
                # DERIVED from team_id, never read from tool_args and never
                # carried beside it in state. The model must not be able to
                # name which team's checkout it is standing in — the same rule
                # agent/tools.py already applies to team_id itself — and a
                # second copy of the path is a second source of truth that can
                # disagree with the first.
                team_id = (ctx.state or {}).get("team_id")
                if not team_id:
                    raise CapabilityError(
                        f"{name} touches the filesystem but this turn carries"
                        " no team, so there is no workspace to scope it to."
                    )
                check_path(
                    str(tool_args[policy.path_arg]),
                    policy,
                    root=workspace_for(str(team_id)),
                    writing=spec.writes,
                )
            if policy.command_arg and policy.command_arg in tool_args:
                check_command(str(tool_args[policy.command_arg]), policy.commands)
            if spec.writes:
                # The RATE dimension, per turn. A loop that has decided to
                # rewrite the repository should be stopped by arithmetic
                # rather than noticed afterwards. State is per-turn because
                # the ADK session is (agent/runtime.py builds a fresh one).
                used = int(ctx.state.get("writes_used", 0)) + 1
                if used > policy.max_writes_per_turn:
                    raise CapabilityError(
                        f"this turn has already written"
                        f" {policy.max_writes_per_turn} times, which is its"
                        " limit. Say what is left to do rather than"
                        " continuing."
                    )
                ctx.state["writes_used"] = used
        except (CapabilityError, WorkspaceError) as exc:
            return {
                "error": "refused_by_capability_budget",
                "tool": name,
                "reason": str(exc),
            }
        return None
