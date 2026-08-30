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

from agent.registry import spec_for


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
            # §15.5: not this gate's business. A sandbox with no credentials
            # and no unproxied network gains nothing from per-command consent.
            return None
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
