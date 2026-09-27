"""fix.md F64 — the scenario spoke the pre-durable-turn API.

🔴 THE DEFECT, in two halves.

* `sim/scenario.py` posted `{team_id, text, thread_type}`. `TurnRequest`
  requires a `thread_id` and has no `thread_type` at all, so every ask would
  have been rejected 422 — and `messages.thread_type` was dropped in the same
  move, so the plain-chat lines raised before that.
* Even with a thread id, it read `reply` and `user_message_id` off the
  response. `POST /agent/turn` only ADMITS a turn: `TurnResponse` is `run_id`
  and `status`, and agent.worker executes it afterwards. So the scenario
  recorded the empty string as every answer, immediately, and its evidence
  could not tell a finished turn from a queued one.

The models are the real ones, imported from `server.app`. The transport is a
double: what is under test is the protocol the scenario speaks, not httpx.
"""
import json
import time
import uuid

import pytest

from sim import scenario


TEAM = str(uuid.uuid4())
THREAD = str(uuid.uuid4())
PERSON = {"id": str(uuid.uuid4()), "name": "Tester", "token": "tok", "tag": "t"}


# ---------------------------------------------------------------------------
# The request and response models, as the server defines them
# ---------------------------------------------------------------------------

def test_the_old_payload_is_rejected_and_names_what_is_missing():
    """The exact shape the scenario used to send, through the real model."""
    from pydantic import ValidationError

    from server.app import TurnRequest

    with pytest.raises(ValidationError) as refused:
        TurnRequest(team_id=TEAM, text="hello", thread_type="group")

    missing = [(e["loc"], e["type"]) for e in refused.value.errors()]
    assert (("thread_id",), "missing") in missing, missing

    # And the shape the scenario sends now is accepted.
    accepted = TurnRequest(team_id=TEAM, text="hello", thread_id=THREAD)
    assert str(accepted.thread_id) == THREAD


def test_the_admission_response_carries_no_answer():
    """Why waiting is not optional: there is nothing to read off the POST."""
    from server.app import TurnResponse

    assert set(TurnResponse.model_fields) == {"run_id", "status"}


# ---------------------------------------------------------------------------
# The protocol the scenario speaks
# ---------------------------------------------------------------------------

class _Stream:
    """Enough of a streaming httpx response for the code under test."""

    def __init__(self, frames, status_code=200, text="", delay=0.0, raises=None):
        self.frames = frames
        self.status_code = status_code
        self.text = text
        #: Seconds before EACH frame. A real run emits a heartbeat every 15s of
        #: silence, which is what keeps a read timeout from ever firing.
        self.delay = delay
        #: Raised once the frames run out — a connection that drops mid-run,
        #: after the opening frame has already told us the run id.
        self.raises = raises

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.text.encode()

    def iter_lines(self):
        for frame in self.frames:
            if self.delay:
                time.sleep(self.delay)
            yield json.dumps(frame)
        if self.raises is not None:
            raise self.raises


class _Post:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"status": "approved"}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


@pytest.fixture
def turn(monkeypatch):
    """A scenario wired to a recorded transport and one fixed thread."""
    monkeypatch.setattr(scenario, "WHO", {"t": PERSON})
    monkeypatch.setattr(scenario, "TEAM", TEAM)
    monkeypatch.setattr(scenario, "_thread_for", lambda person, thread="group": THREAD)
    monkeypatch.setattr(scenario, "CHECK_MODE", True)
    monkeypatch.setattr(scenario, "TRANSCRIPT", [])

    calls = {"stream": [], "post": []}

    def _next(queue):
        """A queued response, or a queued failure to raise instead."""
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def record_stream(method, url, **kwargs):
        calls["stream"].append((method, url, kwargs))
        return _next(calls["streams"])

    def record_post(url, **kwargs):
        calls["post"].append((url, kwargs))
        return _next(calls["posts"])

    calls["streams"] = []
    calls["posts"] = []
    monkeypatch.setattr(scenario.httpx, "stream", record_stream)
    monkeypatch.setattr(scenario.httpx, "post", record_post)
    return calls


def _done_run(run_id, text="Rendering is a plain text grid.", start_seq=0):
    return [
        {"type": "run", "run_id": run_id, "status": "queued"},
        {"type": "status", "run_id": run_id, "status": "running"},
        {"type": "text", "seq": start_seq, "text": text},
        {"type": "done", "run_id": run_id, "status": "done", "detail": None},
    ]


def test_a_finished_turn_records_the_answer_it_actually_produced(turn):
    run_id = str(uuid.uuid4())
    turn["streams"] = [_Stream(_done_run(run_id))]

    out = scenario.ask("t", "what did we decide about rendering?")

    method, url, kwargs = turn["stream"][0]
    assert (method, url) == ("POST", f"{scenario.API}/agent/turn/stream")
    # 🔴 The thread ID, and no thread_type: the two halves of the defect.
    assert kwargs["json"] == {"team_id": TEAM, "text": "what did we decide"
                              " about rendering?", "thread_id": THREAD}
    assert out["status"] == "done"
    assert out["reply"] == "Rendering is a plain text grid."
    entry = scenario.TRANSCRIPT[-1]
    assert entry["status"] == "done"
    assert entry["reply"] == "Rendering is a plain text grid."


def test_a_queued_run_is_never_recorded_as_a_finished_answer(turn):
    """🔴 The precise failure. The old code read the ADMISSION and moved on, so
    a turn that had not started yet went into the evidence as an answered one
    with an empty reply — and the scorer's "did every run end done" check was
    reading a status the scenario had never waited for."""
    run_id = str(uuid.uuid4())
    turn["streams"] = [_Stream([{"type": "run", "run_id": run_id,
                                 "status": "queued"}])]

    with pytest.raises(RuntimeError) as unsettled:
        scenario.ask("t", "what tasks are open?")

    assert "never settled" in str(unsettled.value), unsettled.value
    assert scenario.TRANSCRIPT[-1]["status"] == "queued"
    assert scenario.TRANSCRIPT[-1]["reply"] == ""


def test_a_permission_wait_is_approved_and_the_run_is_followed_through(turn):
    """The consent path, end to end, which is also the post-approval
    continuation: park on a card, approve it as the member who asked, reattach
    from the cursor already seen, and finish."""
    run_id = str(uuid.uuid4())
    consent_id = str(uuid.uuid4())
    parked = [
        {"type": "run", "run_id": run_id, "status": "queued"},
        {"type": "tool_call", "seq": 0, "tool": "task_create"},
        {"type": "tool_result", "seq": 1, "tool": "task_create",
         "response": {"consent_id": consent_id, "status": "needs_permission"}},
        {"type": "status", "run_id": run_id, "status": "waiting_for_permission"},
    ]
    turn["streams"] = [_Stream(parked),
                       _Stream(_done_run(run_id, "Created the task.", start_seq=2))]
    turn["posts"] = [_Post()]

    out = scenario.ask("t", "create the three tasks we agreed")

    # Approved as the asking member, against the card the run parked on.
    (url, kwargs), = turn["post"]
    assert url == f"{scenario.API}/consent/{consent_id}/approve"
    assert kwargs["json"] == {"team_id": TEAM}
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
    # Reattached to the SAME run, from the cursor already folded in — replaying
    # from zero would count every step twice.
    method, reattach_url, reattach = turn["stream"][1]
    assert (method, reattach_url) == (
        "GET", f"{scenario.API}/agent/runs/{run_id}/stream")
    assert reattach["params"] == {"team_id": TEAM, "after_seq": 1}
    assert out["status"] == "done"
    assert scenario.TRANSCRIPT[-1]["approvals"] == 1


def test_a_failed_run_is_not_reported_as_an_answer(turn):
    run_id = str(uuid.uuid4())
    turn["streams"] = [_Stream([
        {"type": "run", "run_id": run_id, "status": "queued"},
        {"type": "done", "run_id": run_id, "status": "failed",
         "detail": "the model returned nothing"},
    ])]

    with pytest.raises(RuntimeError) as failed:
        scenario.ask("t", "anything")

    assert "failed" in str(failed.value)
    assert "the model returned nothing" in str(failed.value)


def test_a_rejected_admission_is_recorded_as_an_http_error(turn):
    """score_team_scenario fails a run with any http_errors, so the shape of
    this entry is load-bearing."""
    turn["streams"] = [_Stream([], status_code=422,
                               text='{"detail":[{"loc":["thread_id"]}]}')]

    with pytest.raises(RuntimeError):
        scenario.ask("t", "anything")

    assert scenario.TRANSCRIPT[-1]["kind"] == "error"
    assert "422" in scenario.TRANSCRIPT[-1]["detail"]


# ---------------------------------------------------------------------------
# The whole-turn deadline
# ---------------------------------------------------------------------------

def test_heartbeats_do_not_carry_a_turn_past_its_deadline(turn, monkeypatch):
    """🔴 THE DEFECT (fix.md F64, second pass). The deadline was consulted only
    BETWEEN permission waits, and `_consume` never looked at it.

    `httpx.Timeout(900)` is an inactivity timeout on reads, not a 900-second
    cap on a streamed response — and `_run_frames` emits a heartbeat every 15
    seconds of silence, which resets it forever. So a queued or stuck run held
    the stream open indefinitely, and the review's reproduction returned
    SUCCESS past the limit: `done elapsed 0.062 limit 0.01`.

    Pinned here in both directions: the turn stops, and the `done` that arrives
    after the budget is not recorded as a completed answer.
    """
    monkeypatch.setattr(scenario, "TURN_TIMEOUT_SECONDS", 0.05)
    run_id = str(uuid.uuid4())
    turn["streams"] = [_Stream([
        {"type": "run", "run_id": run_id, "status": "queued"},
        {"type": "heartbeat", "run_id": run_id},
        {"type": "heartbeat", "run_id": run_id},
        {"type": "done", "run_id": run_id, "status": "done"},
    ], delay=0.04)]
    turn["posts"] = [_Post()]

    with pytest.raises(RuntimeError) as expired:
        scenario.ask("t", "what tasks are open?")

    assert "exceeded 0.05s" in str(expired.value), expired.value
    entry = scenario.TRANSCRIPT[-1]
    assert entry["kind"] == "error", entry
    assert "done" not in entry["detail"].split("last status")[1], entry
    # 🔴 The run it owned is CANCELLED, not abandoned: when the budget goes it
    # is usually still executing, and a scenario that walks away leaves a
    # worker writing into the team the next ask is about to read.
    (url, kwargs), = turn["post"]
    assert url == f"{scenario.API}/agent/runs/{run_id}/cancel"
    assert kwargs["json"] == {"team_id": TEAM}


def test_a_resumed_segment_gets_only_what_is_left_of_the_budget(turn, monkeypatch):
    """Each reattach used to receive the FULL original timeout, so a turn with
    four approvals could legitimately run four times its stated limit."""
    monkeypatch.setattr(scenario, "TURN_TIMEOUT_SECONDS", 0.5)
    run_id = str(uuid.uuid4())
    consent_id = str(uuid.uuid4())
    turn["streams"] = [
        # Spends about 0.12s of the budget before it parks, so "the remainder"
        # and "the whole thing" are different numbers rather than the same one
        # to within floating-point noise.
        _Stream([
            {"type": "run", "run_id": run_id, "status": "queued"},
            {"type": "tool_result", "seq": 1, "tool": "task_create",
             "response": {"consent_id": consent_id}},
            {"type": "status", "run_id": run_id,
             "status": "waiting_for_permission"},
        ], delay=0.04),
        _Stream(_done_run(run_id, "Created the task.", start_seq=2), delay=0.4),
    ]
    turn["posts"] = [_Post(), _Post()]

    with pytest.raises(RuntimeError) as expired:
        scenario.ask("t", "create the three tasks we agreed")

    assert "exceeded" in str(expired.value), expired.value
    # Approved, then the resumed stream ran out and the run was cancelled.
    assert turn["post"][0][0].endswith("/approve"), turn["post"]
    assert turn["post"][1][0].endswith("/cancel"), turn["post"]
    # And the reattach asked httpx for the REMAINDER, not the whole budget —
    # which is what it used to be handed, every time, however long the turn had
    # already taken.
    _, _, reattach = turn["stream"][1]
    assert reattach["timeout"].read <= scenario.TURN_TIMEOUT_SECONDS - 0.1, (
        reattach["timeout"])


def test_a_stream_that_goes_silent_is_a_timeout_not_a_transport_error(turn,
                                                                      monkeypatch):
    """The other shape. `_consume`'s in-loop check handles a noisy stream; a
    read timeout is what a completely quiet one produces, and it must be
    reported as the turn running out rather than as the network breaking."""
    monkeypatch.setattr(scenario, "TURN_TIMEOUT_SECONDS", 0.05)
    run_id = str(uuid.uuid4())

    class _Silent(_Stream):
        def iter_lines(self):
            raise scenario.httpx.ReadTimeout("nothing came back")

    turn["streams"] = [_Silent([])]
    turn["posts"] = [_Post()]

    with pytest.raises(RuntimeError) as expired:
        scenario.ask("t", "anything")

    assert "exceeded" in str(expired.value), expired.value
    assert "no run to cancel" in str(expired.value), expired.value


# ---------------------------------------------------------------------------
# Giving up on a turn without leaving its run behind
# ---------------------------------------------------------------------------

def _parked(run_id, consent_id):
    return [
        {"type": "run", "run_id": run_id, "status": "queued"},
        {"type": "tool_result", "seq": 1, "tool": "task_create",
         "response": {"consent_id": consent_id}},
        {"type": "status", "run_id": run_id, "status": "waiting_for_permission"},
    ]


def test_an_approval_whose_answer_is_lost_cancels_the_run_it_owns(turn):
    """🔴 THE DEFECT (fix.md F64, third pass). The approval POST sat outside
    any handler, so `httpx.TimeoutException` unwound straight out of `ask`:
    no cancellation of the run, and no evidence either — `http_errors` is
    written by `_note_failure`, which was never reached.

    And the lost answer is the dangerous shape, not a harmless one:
    `approve_consent` requeues the run inside the same call, so the server may
    well have approved the card and started the turn running again. The
    scenario cannot retry the POST — that is blindly repeating a side effect —
    so it records the uncertainty and stops the run it knows about.
    """
    run_id = str(uuid.uuid4())
    consent_id = str(uuid.uuid4())
    turn["streams"] = [_Stream(_parked(run_id, consent_id))]
    turn["posts"] = [scenario.httpx.ReadTimeout("no answer"), _Post()]

    with pytest.raises(RuntimeError) as aborted:
        scenario.ask("t", "create the three tasks we agreed")

    message = str(aborted.value)
    assert "unknown whether the card was approved" in message, message
    # The evidence the scorer reads, not just an exception.
    entry = scenario.TRANSCRIPT[-1]
    assert entry["kind"] == "error", entry
    # And the run this turn owns was stopped, by id.
    assert turn["post"][0][0].endswith(f"/consent/{consent_id}/approve")
    assert turn["post"][1][0] == f"{scenario.API}/agent/runs/{run_id}/cancel"
    assert turn["post"][1][1]["json"] == {"team_id": TEAM}


def test_a_cancellation_that_also_fails_stays_in_the_evidence(turn):
    """"We could not stop it" is the part an operator needs; swallowing it
    would leave the report claiming a tidy abort."""
    run_id = str(uuid.uuid4())
    consent_id = str(uuid.uuid4())
    turn["streams"] = [_Stream(_parked(run_id, consent_id))]
    turn["posts"] = [scenario.httpx.ReadTimeout("no answer"),
                     scenario.httpx.ConnectError("cancel unreachable")]

    with pytest.raises(RuntimeError) as aborted:
        scenario.ask("t", "create the three tasks we agreed")

    assert "cancel failed" in str(aborted.value), aborted.value
    assert "cancel failed" in scenario.TRANSCRIPT[-1]["detail"]


def test_a_transport_failure_after_admission_cancels_the_known_run(turn):
    """The same policy, reached the other way: the opening `run` frame has
    already told us the id when the connection drops."""
    run_id = str(uuid.uuid4())
    turn["streams"] = [_Stream([{"type": "run", "run_id": run_id,
                                 "status": "running"}],
                               raises=scenario.httpx.ConnectError("dropped"))]
    turn["posts"] = [_Post()]

    with pytest.raises(RuntimeError) as aborted:
        scenario.ask("t", "what tasks are open?")

    assert "stream failed" in str(aborted.value), aborted.value
    (url, _), = turn["post"]
    assert url == f"{scenario.API}/agent/runs/{run_id}/cancel"


def test_a_refused_admission_has_no_run_to_cancel(turn):
    """The control. An abort before any run exists must not invent one to
    cancel, and must still record the failure."""
    turn["streams"] = [_Stream([], status_code=422, text='{"detail":"nope"}')]

    with pytest.raises(RuntimeError) as refused:
        scenario.ask("t", "anything")

    assert "no run to cancel" in str(refused.value), refused.value
    assert not turn["post"], turn["post"]
    assert scenario.TRANSCRIPT[-1]["kind"] == "error"
