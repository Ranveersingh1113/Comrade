"""Comrade must run its own work before asking a human to review it.

🔴 THE RUN THIS EXISTS FOR. A four-person scenario asked Comrade to write a
snake game and propose it as a pull request. It did: 2,992 characters, a
SnakeGame class, movement, growth, collision. It never executed a line of it.
The patch contains a real off-by-one — self-collision is checked before the
tail is popped, so moving into the square the tail is vacating is a loss — and
one `python snake.py` would have shown the game running while `pytest` showed
nothing to run.

A reviewer's time is the scarce thing in this product. A pull request nobody
has run spends it on a question the author could have answered.

THE GATE IS A GENERATION COUNTER, NOT A BOOLEAN
------------------------------------------------
`repo_edit` bumps `repo_edit_generation`. A `repo_run` that actually completed
with exit code 0 stamps the generation it saw into `repo_verified_generation`.
`repo_propose_pr` refuses while those two disagree.

A boolean would say "verified" forever after one passing run, and the failure
mode that matters is edit -> run -> edit -> propose: the second edit is the
unverified one, and it is exactly the "one last small fix" that breaks things.
Comparing generations makes a later edit invalidate an earlier pass for free.

Both keys are written by the server, in runtime.py, and updated only by the
tools' own success paths. The model cannot set either — it could otherwise
declare its own work verified, which is the whole thing this prevents.
"""
from types import SimpleNamespace

import subprocess

import pytest

from agent.repo_tools import repo_edit, repo_propose_pr, repo_run
from shared.workspace import repo_checkout, thread_checkout

TEAM_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REQUESTER = "a1a1a1a1-0000-0000-0000-000000000001"
REPO = "acme/app"
# The agent works in a THREAD's tree, not the team's (Task 15).
THREAD_A = "cccccccc-cccc-cccc-cccc-cccccccccccc"

REFUSAL = "Run a relevant test, build, lint, or executable check"


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root", str(tmp_path / "ws")
    )
    root = thread_checkout(TEAM_A, THREAD_A, REPO)
    (root / "src").mkdir(parents=True)
    (root / "src" / "auth.py").write_text("def authenticate(user):\n    return True\n")
    # A REAL repository. The fake `.git` directory this used to write is the
    # malformed state capture_patch now refuses — git rejects it and walks UP
    # looking for a real one, which is how `git add -A` reached the
    # developer's own home directory. A test that fakes it tests nothing.
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
    """The state the server binds. Both generation keys start where runtime.py
    puts them, so a test cannot accidentally pass by omitting one."""
    return SimpleNamespace(state={
        "team_id": TEAM_A,
        "requester_id": REQUESTER,
        "thread_id": THREAD_A,
        "repo_full_name": REPO,
        "repo_edit_generation": 0,
        "repo_verified_generation": None,
    })


@pytest.fixture(autouse=True)
def no_git(monkeypatch):
    """Stand in for capture_patch, so these tests never shell out to git.

    Not only for speed. A checkout whose .git is not a VALID repository sends
    `git add -A` walking up the directory tree until it finds one — and the
    workspaces root lives under the user's home directory, which on a
    developer machine is itself a repository. The first run of this file
    reported:

        fatal: Unable to create 'C:/Users/ricky/.git/index.lock': File exists.

    capture_patch guarded with `(checkout / ".git").exists()`, which a
    malformed .git satisfies and git does not. FIXED in Task 15 — it now asks
    git for `--show-toplevel` under GIT_CEILING_DIRECTORIES — and the fixture
    above builds a real repository rather than a fake one. The stand-in stays
    because this gate is about bookkeeping across three tools, and shelling out
    to git to prove an integer changed makes it slow for nothing.
    """
    from pipeline.repo_pr import PullRequestError

    def _fake_capture(team_id, repo_full_name, thread_id):
        raise PullRequestError("nothing has changed, so there is nothing to open.")

    monkeypatch.setattr("pipeline.repo_pr.capture_patch", _fake_capture)


@pytest.fixture
def sandbox(monkeypatch):
    """Swap the container out. This gate is about bookkeeping across three
    tools, and booting Docker to prove an integer changed would make the test
    slow and flaky for nothing."""
    calls = {"exit_code": 0, "timed_out": False}

    def _fake_run(argv, *, root, deps=None, **_kw):
        return {"exit_code": calls["exit_code"], "stdout": "", "stderr": "",
                "timed_out": calls["timed_out"]}

    monkeypatch.setattr("agent.repo_tools.run_contained", _fake_run)
    return calls


def _edit(ctx, new="return verify(user)"):
    return repo_edit("src/auth.py", "return True", new, ctx)


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------

def test_an_edit_then_a_proposal_is_refused(checkout, ctx):
    _edit(ctx)
    result = repo_propose_pr("Add verification", "body", ctx)
    assert REFUSAL in result.get("error", ""), result


def test_a_failed_run_is_not_verification(checkout, ctx, sandbox):
    """repo_run's own docstring says a non-zero exit is a normal answer, and
    it is — for reporting. It is not evidence the change works, which is the
    only question this gate asks."""
    _edit(ctx)
    sandbox["exit_code"] = 1
    repo_run("pytest -q", ctx)
    result = repo_propose_pr("Add verification", "body", ctx)
    assert REFUSAL in result.get("error", ""), result


def test_a_timed_out_run_is_not_verification(checkout, ctx, sandbox):
    """A command the sandbox killed produced no verdict. exit_code is None
    there, so a truthiness check would read it as a pass."""
    _edit(ctx)
    sandbox["exit_code"] = None
    sandbox["timed_out"] = True
    repo_run("pytest -q", ctx)
    result = repo_propose_pr("Add verification", "body", ctx)
    assert REFUSAL in result.get("error", ""), result


@pytest.mark.parametrize("command", ["python --version", "make"])
def test_an_inspection_or_untargeted_build_does_not_count_as_verification(
    checkout, ctx, sandbox, command
):
    _edit(ctx)
    repo_run(command, ctx)
    result = repo_propose_pr("Add verification", "body", ctx)
    assert REFUSAL in result.get("error", ""), result


def test_a_passing_run_lets_the_proposal_through(checkout, ctx, sandbox):
    """Past the gate. It fails later, on the real git checkout this fixture
    does not build — asserting the refusal is ABSENT is the whole point, since
    asserting success would test capture_patch instead of the gate."""
    _edit(ctx)
    repo_run("pytest -q", ctx)
    result = repo_propose_pr("Add verification", "body", ctx)
    assert REFUSAL not in str(result), result


def test_a_second_edit_invalidates_an_earlier_pass(checkout, ctx, sandbox):
    """🔴 The case a boolean would miss, and the one that actually happens:
    run the tests, then make one more small change, then propose."""
    _edit(ctx)
    repo_run("pytest -q", ctx)
    # Replaces what the FIRST edit wrote. Searching for "return True" again
    # would fail to match, produce no write, and correctly not bump the
    # generation — the test would then pass while proving nothing.
    second = repo_edit(
        "src/auth.py", "return verify(user)",
        "return verify(user)  # one last tweak", ctx,
    )
    assert second.get("replaced") is True, second
    result = repo_propose_pr("Add verification", "body", ctx)
    assert REFUSAL in result.get("error", ""), result


def test_proposing_without_editing_is_not_this_refusal(checkout, ctx):
    """Nothing was edited, so there is nothing to have verified. The empty
    diff is what refuses this, and it says something a member can act on —
    replacing it with "go run a test" would send them looking for a change
    that was never made."""
    result = repo_propose_pr("Nothing", "body", ctx)
    assert REFUSAL not in str(result), result


# ---------------------------------------------------------------------------
# The model cannot vouch for itself
# ---------------------------------------------------------------------------

def test_a_failed_edit_does_not_bump_the_generation(checkout, ctx, sandbox):
    """Only a write that happened counts. Otherwise a refused edit would
    invalidate a genuine verification and the agent would be stuck re-running
    tests for a change it never made."""
    _edit(ctx)
    repo_run("pytest -q", ctx)
    failed = repo_edit("src/auth.py", "text that is not there", "x", ctx)
    assert "error" in failed
    result = repo_propose_pr("Add verification", "body", ctx)
    assert REFUSAL not in str(result), result


def test_a_refused_run_does_not_count_as_verification(checkout, ctx, monkeypatch):
    """repo_run returns {"error": ...} without running anything when the
    command is refused by the allowlist. Nothing executed, so nothing is
    verified — the same shape as the scorer's rule for reading these steps
    back out of the database."""
    _edit(ctx)
    result_run = repo_run("pytest -q && rm -rf /", ctx)
    assert "error" in result_run
    result = repo_propose_pr("Add verification", "body", ctx)
    assert REFUSAL in result.get("error", ""), result
