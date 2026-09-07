"""What has to be true before a change can be proposed as a pull request.

🔴 THE DEFECTS.

Verification was an in-memory COUNTER. `repo_edit` incremented
`repo_edit_generation`; a `repo_run` that exited 0 stamped the current count
into `repo_verified_generation`; and the proposal gate compared the two. So it
knew that "a run happened after the last edit" and nothing about WHAT was
checked or WHAT it was checked against.

Three ways through it, all of them ordinary:

  * `python -c 'pass'` exits 0. So does `true`, and `echo ok`. Any of them
    marked the tree verified — the plan names this one exactly.
  * A COMMAND can change the tree. A formatter, a codegen step, a build that
    writes files: none of them touch the edit counter, so the check that ran
    before the mutation still vouched for the tree after it.
  * A restart rebuilds the ADK session, so a check made in a previous
    attempt left no record — and the counter, also rebuilt, could not tell
    "checked earlier" from "checked never".

The fix binds verification to the PATCH — what is actually being proposed —
rather than to a count of tool calls.
"""
import pytest


class _Ctx:
    """A tool context with just the state the repository tools read."""

    def __init__(self, edits=1, **state):
        self.state = {
            "team_id": "team", "requester_id": "member", "thread_id": "thread",
            "repo_full_name": "acme/app", "repo_edit_generation": edits,
            **state,
        }


# ---------------------------------------------------------------------------
# What counts as a check
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "python -c 'pass'",
    "python -c pass",
    "true",
    ":",
    "echo ok",
    "node -e ''",
])
def test_a_command_that_cannot_observe_the_project_is_not_verification(command):
    """🔴 `python -c 'pass'` exits 0 and proves nothing. It marked the tree
    verified, and the plan names it by name."""
    import shlex

    from agent.repo_tools import _is_verification_command

    assert not _is_verification_command(shlex.split(command)), command


@pytest.mark.parametrize("command", [
    "pytest tests/test_importer.py",
    "npm test",
    "make check",
    "python -m pytest",
    "cargo test",
    "npm run build",
])
def test_a_real_check_still_counts(command):
    """The refusal must not become "nothing is ever verification"."""
    import shlex

    from agent.repo_tools import _is_verification_command

    assert _is_verification_command(shlex.split(command)), command


# ---------------------------------------------------------------------------
# Verification is bound to what is being proposed
# ---------------------------------------------------------------------------

def test_a_check_records_what_it_checked(monkeypatch):
    """🔴 Nothing recorded the command, its exit status, or the tree it ran
    against — only that some run had happened after some edit."""
    from agent import repo_tools

    ctx = _Ctx()
    monkeypatch.setattr(repo_tools, "_patch_digest", lambda *_a, **_k: "digest-1")
    repo_tools._note_verified(ctx, ["pytest", "tests/"], root=None)

    record = ctx.state[repo_tools._VERIFICATION]
    assert record["command"] == "pytest tests/"
    assert record["digest"] == "digest-1"


def test_a_proposal_matching_the_verified_patch_is_allowed(monkeypatch):
    from agent import repo_tools

    ctx = _Ctx()
    monkeypatch.setattr(repo_tools, "_patch_digest", lambda *_a, **_k: "digest-1")
    repo_tools._note_verified(ctx, ["pytest"], root=None)

    assert repo_tools._unverified(ctx, root=None) is False


def test_an_edit_after_the_check_invalidates_it(monkeypatch):
    """The case the counter did handle, and still has to."""
    from agent import repo_tools

    ctx = _Ctx()
    digests = iter(["digest-1", "digest-2"])
    monkeypatch.setattr(repo_tools, "_patch_digest", lambda *_a, **_k: next(digests))
    repo_tools._note_verified(ctx, ["pytest"], root=None)

    assert repo_tools._unverified(ctx, root=None) is True


def test_a_command_that_changes_the_tree_invalidates_its_own_check(monkeypatch):
    """🔴 A formatter, a codegen step, a build that writes files: none of them
    touched the edit counter, so the check that ran BEFORE the mutation still
    vouched for the tree AFTER it."""
    from agent import repo_tools

    ctx = _Ctx()
    state = {"n": 0}

    def _digest(*_a, **_k):
        # The check itself rewrites the tree while it runs.
        state["n"] += 1
        return f"digest-{state['n']}"

    monkeypatch.setattr(repo_tools, "_patch_digest", _digest)
    repo_tools._note_verified(ctx, ["make", "fmt-and-test"], root=None)

    assert repo_tools._unverified(ctx, root=None) is True


def test_edits_with_no_verification_record_are_unverified(monkeypatch):
    """Edited and never checked — including after a restart, where the ADK
    session is rebuilt and any record of a check made in a previous attempt is
    gone with it."""
    from agent import repo_tools

    ctx = _Ctx()  # edits made, no record: a fresh session over an edited tree
    monkeypatch.setattr(repo_tools, "_patch_digest", lambda *_a, **_k: "digest-1")

    assert repo_tools._unverified(ctx, root=None) is True


def test_a_turn_that_changed_nothing_is_not_this_refusal(monkeypatch):
    """Nothing was edited THIS TURN, so "go run a test" would send a member
    looking for a change that was never made. The empty diff refuses that, and
    says something they can act on.

    A checkout can also carry work this turn did not do — a tree is not
    guaranteed clean — and demanding the agent verify somebody else's
    uncommitted files is not an improvement.
    """
    from agent import repo_tools

    ctx = _Ctx(edits=0)
    monkeypatch.setattr(repo_tools, "_patch_digest", lambda *_a, **_k: "digest-1")

    assert repo_tools._unverified(ctx, root=None) is False
