"""ADK calls after-tool callbacks even when before-tool returned a cached/refused result."""
import asyncio

import pytest
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent.permission_plugin import ChokepointPlugin
from agent.run_queue import enqueue_turn, claim_next_run
from tests._seed import TEAM_A, A1
from tests.test_agent_resume import _thread_id


@pytest.mark.parametrize("mode", ["cached", "refused", "retry"])
def test_real_adk_after_callback_respects_effect_ownership(seeded, mode):
    refused = mode == "refused"
    calls = []

    def team_propose_task(title: str) -> dict:
        """A test write counted exactly once."""
        calls.append(title)
        return {"consent_id": "cached-card"}

    def repo_edit(path: str, old_text: str, new_text: str) -> dict:
        """Must never execute for a secret path."""
        calls.append(path)
        return {"path": path}

    def repo_propose_pr(title: str) -> dict:
        """The first precondition refusal did not create a proposal."""
        calls.append(title)
        if len(calls) == 1:
            return {"error": "Run a check first", "effect_not_started": True}
        return {"consent_id": "new-card"}

    selected = repo_edit if refused else (repo_propose_pr if mode == "retry" else team_propose_task)
    name = selected.__name__
    args = {"path": ".env", "old_text": "", "new_text": "x"} if refused else {"title": "Once"}

    class ScriptedModel(BaseLlm):
        count: int = 0

        async def generate_content_async(self, llm_request, stream=False):
            self.count += 1
            part = (types.Part(function_call=types.FunctionCall(name=name, args=args))
                    if self.count <= 2 else types.Part(text="Done"))
            yield LlmResponse(content=types.Content(role="model", parts=[part]))

    thread = _thread_id()
    admitted = enqueue_turn(TEAM_A, A1, thread, "test callback lifecycle")
    run = claim_next_run("callback-worker")
    assert run.id == admitted

    async def drive():
        agent = LlmAgent(name="test_agent", model=ScriptedModel(model="scripted"),
                         tools=[selected])
        runner = Runner(app=App(name="callback_test", root_agent=agent,
                               plugins=[ChokepointPlugin()]),
                        session_service=InMemorySessionService())
        session = await runner.session_service.create_session(
            app_name="callback_test", user_id=A1,
            state={"team_id": TEAM_A, "requester_id": A1, "thread_id": thread,
                   "agent_run_id": run.id, "worker_id": "callback-worker"})
        return [event async for event in runner.run_async(
            user_id=A1, session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="Go")]))]

    events = asyncio.run(drive())
    responses = [part.function_response.response for event in events
                 for part in (event.content.parts if event.content else [])
                 if part.function_response]
    assert len(responses) == 2
    assert calls == ([] if refused else (["Once", "Once"] if mode == "retry" else ["Once"]))
    if refused:
        assert all(result["error"] == "refused_by_capability_budget" for result in responses)
    elif mode == "retry":
        assert responses == [{"error": "Run a check first", "effect_not_started": True}, {"consent_id": "new-card"}]
    else:
        assert responses == [{"consent_id": "cached-card"}] * 2
