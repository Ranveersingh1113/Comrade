"""HTTP entrypoint for the Comrade agent.

POST /agent/turn runs one agent turn and returns its reply + run id. team_id and
requester_id are read into server-side variables here (the binding point); a
later auth task will source them from the validated Supabase JWT instead of the
request body. The model never receives them as tool arguments.
"""
from fastapi import FastAPI
from pydantic import BaseModel

from agent.runtime import run_turn_sync

app = FastAPI(title="Comrade Agent Runtime")


class TurnRequest(BaseModel):
    team_id: str
    requester_id: str
    text: str


class TurnResponse(BaseModel):
    run_id: str
    reply: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/agent/turn", response_model=TurnResponse)
def agent_turn(req: TurnRequest) -> TurnResponse:
    # Server-bound here; swap the source to JWT claims in the auth task.
    team_id = req.team_id
    requester_id = req.requester_id
    result = run_turn_sync(team_id, requester_id, req.text)
    return TurnResponse(run_id=result["run_id"], reply=result["reply"])
