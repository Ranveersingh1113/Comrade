"""Does the rolling summary actually keep what the thread established?

The plan's first check for T19 is "important constraint survives 100+
messages". Everything else about compaction is testable with `_summarise`
stubbed; whether the SUMMARY is any good is not, and a durable working memory
that quietly loses the constraint is worse than none — the agent would carry a
confident, incomplete account of the thread into every later turn.
"""
import pytest

from pipeline.compaction import MAX_SUMMARY_CHARS, _summarise


def _thread_of(n: int) -> list[dict]:
    """A realistic-shaped thread: two constraints early, then a lot of noise,
    a correction in the middle, and an open question near the end."""
    messages = [
        {"sender": "Ann", "text": "before anything else: we are NOT touching the"
                                  " vendored fork, it is unmaintained"},
        {"sender": "Bo", "text": "agreed. also the customer is still on"
                                 " Postgres 14, so no 15-only syntax"},
        {"sender": "Ann", "text": "we will ship the importer first"},
    ]
    for i in range(n):
        messages.append({"sender": "Bo" if i % 2 else "Ann",
                         "text": f"looking at the parser now, {i} files left"})
    messages += [
        {"sender": "Ann", "text": "correction: the importer is second, the"
                                  " exporter ships first"},
        {"sender": "Bo", "text": "who is signing off the schema change?"},
    ]
    return messages


@pytest.mark.live
def test_a_constraint_stated_first_survives_a_hundred_messages(capsys):
    summary = _summarise("", _thread_of(100))
    with capsys.disabled():
        print(f"\n--- summary ({len(summary)} chars) ---\n{summary}\n")

    lowered = summary.lower()
    assert "fork" in lowered, "the thread's first constraint was lost"
    assert "14" in lowered or "postgres" in lowered, (
        "the version constraint was lost"
    )


@pytest.mark.live
def test_the_summary_keeps_the_correction_not_the_thing_corrected(capsys):
    summary = _summarise("", _thread_of(100))
    with capsys.disabled():
        print(f"\n--- summary ---\n{summary}\n")

    lowered = summary.lower()
    # The thread said importer-first, then corrected itself to exporter-first.
    # Carrying both forward is how an agent confidently does the wrong one.
    assert "exporter" in lowered


@pytest.mark.live
def test_the_summary_stays_inside_its_budget(capsys):
    """It is paid on EVERY later turn of the thread, so an unbounded summary
    is the transcript again, more expensively."""
    summary = _summarise("", _thread_of(200))

    assert len(summary) <= MAX_SUMMARY_CHARS
