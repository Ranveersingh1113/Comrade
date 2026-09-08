"""Proposing work that no check ever saw.

🔴 THE DEFECT (fix.md F40). The gate asked the wrong question.

`_unverified` opens with:

    if tool_context.state.get(_EDIT_GEN, 0) == 0:
        return False

`repo_edit_generation` counts calls to `repo_edit` **in this in-memory turn**.
The reasoning attached to it is sound — a turn that changed nothing has nothing
to check, and an empty diff is refused elsewhere with something a member can
act on. But "this turn called repo_edit" and "there is nothing to propose" are
not the same statement, and there are two ordinary ways for them to disagree:

  * **A resumed or new turn.** State resets, the counter starts at zero, and
    the checkout still carries uncommitted work from before. The gate reads
    that as "nothing to check" when it means "never checked here".
  * **A command that writes.** `repo_run` invoking a formatter, a codegen step,
    a build, or a script produces real changes without `repo_edit` ever being
    called. The counter stays zero; the patch does not.

In both cases `capture_patch` finds a non-empty diff, so the empty-diff refusal
never fires either, and the pull request is proposed with no verification
record and no digest measurement at all.

T23's whole argument is that a reviewer's time is the scarce thing and a patch
nobody has run spends it on a question the author could have answered. This is
the door left open in the wall built for exactly that.

THE FIX IS TO ASK ABOUT THE PATCH. Whether this turn happened to call the edit
tool is bookkeeping; what is being proposed is the thing. A genuinely empty
tree still takes the empty-diff path, because a tree proposing nothing has
nothing to verify.
"""
import subprocess
from types import SimpleNamespace

import pytest

from agent.repo_tools import repo_propose_pr
from shared.workspace import thread_checkout

TEAM_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REQUESTER = "a1a1a1a1-0000-0000-0000-000000000001"
REPO = "acme/app"
THREAD_A = "cccccccc-cccc-cccc-cccc-cccccccccccc"

REFUSAL = "Run a relevant test, build, lint, or executable check"


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A real repository with one commit, clean."""
    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root", str(tmp_path / "ws")
    )
    root = thread_checkout(TEAM_A, THREAD_A, REPO)
    (root / "src").mkdir(parents=True)
    (root / "src" / "auth.py").write_text(
        "def authenticate(user):\n    return True\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@test.dev", "-c", "user.name=Test",
         "commit", "-q", "-m", "initial"],
        cwd=root, check=True, capture_output=True,
    )
    return root


@pytest.fixture
def ctx():
    """Exactly what runtime.py binds at the start of a turn: no edits yet."""
    return SimpleNamespace(state={
        "team_id": TEAM_A,
        "requester_id": REQUESTER,
        "thread_id": THREAD_A,
        "repo_full_name": REPO,
        "repo_edit_generation": 0,
        "repo_verified_generation": None,
    })


@pytest.fixture(autouse=True)
def proposals(monkeypatch):
    """Record what would have been proposed, and never open anything.

    `propose_action` is where a refused turn and an accepted one visibly
    differ, so it is the observation point rather than a return value that
    could be shaped either way.
    """
    seen: list[dict] = []

    def _propose(**kwargs):
        seen.append(kwargs)
        return {"status": "proposed", "consent_id": "consent-1"}

    # Imported inside the function, so it is patched where it LIVES.
    monkeypatch.setattr("shared.consent.propose_action", _propose)
    monkeypatch.setattr(
        "pipeline.repo_pr.capture_patch",
        lambda team_id, repo_full_name, thread_id: "diff --git a b\n+x\n",
    )
    return seen


# ---------------------------------------------------------------------------

def test_a_dirty_tree_from_a_previous_turn_still_needs_a_check(
    checkout, ctx, proposals,
):
    """🔴 The resumed-turn case. The work is real and uncommitted; the counter
    is zero because THIS turn's state is fresh."""
    (checkout / "src" / "auth.py").write_text(
        "def authenticate(user):\n    return user.is_admin\n", encoding="utf-8")

    result = repo_propose_pr("Fix auth", "please review", ctx)

    assert REFUSAL in result.get("error", ""), (
        "a change no check has seen was proposed because this turn did not"
        " happen to call repo_edit"
    )
    assert proposals == []


def test_a_change_made_by_a_command_still_needs_a_check(
    checkout, ctx, proposals,
):
    """The command case. A formatter, a codegen step or a build script writes
    real changes and `repo_edit` is never called."""
    (checkout / "src" / "generated.py").write_text(
        "# written by a build step\nVALUE = 1\n", encoding="utf-8")

    result = repo_propose_pr("Add generated code", "please review", ctx)

    assert REFUSAL in result.get("error", "")
    assert proposals == []


def test_an_untracked_file_alone_is_enough_to_require_a_check(
    checkout, ctx, proposals,
):
    """A new file is the commonest shape of "the agent wrote something", and
    it never appears in `git diff HEAD` at all."""
    (checkout / "snake.py").write_text("print('hi')\n", encoding="utf-8")

    result = repo_propose_pr("Add snake", "please review", ctx)

    assert REFUSAL in result.get("error", "")


def test_a_clean_tree_still_gets_the_empty_change_answer(
    checkout, ctx, proposals, monkeypatch,
):
    """The half that must NOT change. A tree proposing nothing has nothing to
    verify, and "go run a test" would send the member looking for a change
    nobody made."""
    from pipeline.repo_pr import PullRequestError

    monkeypatch.setattr(
        "pipeline.repo_pr.capture_patch",
        lambda *a, **k: (_ for _ in ()).throw(
            PullRequestError("nothing has changed, so there is nothing to open.")),
    )

    result = repo_propose_pr("Nothing", "please review", ctx)

    assert REFUSAL not in result.get("error", "")
    assert "nothing has changed" in result.get("error", "")


def test_a_verified_dirty_tree_is_proposed(checkout, ctx, proposals):
    """And the gate must still open. A recorded verification whose digest
    matches what is being proposed is the whole point of the mechanism."""
    from agent import repo_tools

    (checkout / "src" / "auth.py").write_text(
        "def authenticate(user):\n    return user.is_admin\n", encoding="utf-8")
    # Recorded the way a completed `repo_run` records it, against this tree.
    repo_tools._note_verified(ctx, ["pytest"], root=checkout)

    result = repo_propose_pr("Fix auth", "please review", ctx)

    assert "error" not in result, result
    assert len(proposals) == 1


def test_a_verification_is_invalidated_by_a_later_change(
    checkout, ctx, proposals,
):
    """edit -> run -> edit -> propose. The second change is the unverified one,
    and it is exactly the "one last small fix" that breaks things."""
    from agent import repo_tools

    (checkout / "src" / "auth.py").write_text(
        "def authenticate(user):\n    return user.is_admin\n", encoding="utf-8")
    repo_tools._note_verified(ctx, ["pytest"], root=checkout)
    (checkout / "src" / "auth.py").write_text(
        "def authenticate(user):\n    return True  # oops\n", encoding="utf-8")

    result = repo_propose_pr("Fix auth", "please review", ctx)

    assert REFUSAL in result.get("error", "")
    assert proposals == []
