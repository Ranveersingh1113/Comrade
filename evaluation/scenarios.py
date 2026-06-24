"""Canonical tool-routing scenarios, run against the seeded team
(A1 = leader, A2 = member)."""
from tests._seed import A1, A2, TEAM_A

_STATE = {"team_id": TEAM_A, "requester_id": A1}

SCENARIOS = [
    {
        "name": "summarize_status",
        "prompt": "Give me a quick status summary of the team.",
        "state": _STATE,
        "expected": ["team_get_state"],
        "forbidden": ("team_propose_task", "team_propose_group_message",
                      "member_send_nudge"),
    },
    {
        "name": "create_task_for_member",
        "prompt": "Create a task for A2 to draft the final report.",
        "state": _STATE,
        "expected": ["team_propose_task"],
        "arg_checks": {"team_propose_task": {"assignee_id": A2}},
    },
    {
        "name": "gated_intent_routes_through_proposal",
        "prompt": "Add a task for A2 to set up the GitHub repo.",
        "state": _STATE,
        "expected": ["team_propose_task"],
    },
    {
        "name": "nudge_member_privately",
        "prompt": "Privately check in with A2 about their pending task.",
        "state": _STATE,
        "expected": ["member_send_nudge"],
        "arg_checks": {"member_send_nudge": {"member_id": A2}},
    },
    {
        "name": "post_to_group",
        "prompt": "Post a message to the group room: standup at 5pm today.",
        "state": _STATE,
        "expected": ["team_propose_group_message"],
    },
]
