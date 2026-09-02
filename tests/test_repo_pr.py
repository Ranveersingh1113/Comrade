"""A working copy becomes a pull request a human approved.

Phase C2. The git half runs against a real local bare repository — clone,
branch, apply, commit, push, all of it — because a push path verified only
against a mock is a push path nobody has run. Only `_create_pr` talks to
github.com, and it is a seam.

Two properties carry this file:

  * **The patch is captured at PROPOSE time.** sync_repo resets the checkout at
    the start of every turn, so a member approving an hour later would
    otherwise be approving a tree some intervening turn had already wiped —
    and the executor would push nothing, silently, because an empty diff is not
    an error anywhere.
  * **Executing twice converges.** execute_consent runs this inside a Postgres
    transaction and it does network I/O in the middle, which is the one place
    the exactly-once guarantee cannot reach. A push that lands before a
    rollback leaves a branch the row denies, so the action has to be safe to
    repeat rather than guaranteed to run once.
"""
import subprocess
from pathlib import Path

import psycopg
import pytest

from pipeline.repo_pr import (
    BRANCH_PREFIX, PATCH_MAX_CHARS, PullRequestError, branch_for, capture_patch,
    default_branch, open_pull_request,
)
from shared.config import settings
from shared.workspace import repo_checkout
from tests._seed import TEAM_A

REPO = "acme/app"


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def origin(tmp_path):
    """A real bare repository to push into — the thing a mock cannot be."""
    work = tmp_path / "seed"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@test.dev")
    _git(work, "config", "user.name", "Test")
    (work / "app.py").write_text("print('hello')\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "initial")

    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)],
                   check=True, capture_output=True)
    # The clone went work -> bare, so `work` has no remote. Tests that move the
    # base underneath an approved patch push from here.
    _git(work, "remote", "add", "origin", str(bare))
    return bare


@pytest.fixture
def checkout(tmp_path, monkeypatch, origin):
    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root", str(tmp_path / "ws")
    )
    monkeypatch.setattr("shared.config.settings.github_pat", "unused-locally")
    monkeypatch.setattr("pipeline.repo_sync._url_for", lambda _n: str(origin))
    from pipeline.repo_sync import sync_repo

    return sync_repo(TEAM_A, REPO)


# ---------------------------------------------------------------------------
# Capturing the change
# ---------------------------------------------------------------------------

def test_it_captures_what_the_agent_changed(checkout):
    (checkout / "app.py").write_text("print('goodbye')\n")
    patch = capture_patch(TEAM_A, REPO)
    assert "goodbye" in patch and "hello" in patch


def test_it_captures_a_new_file(checkout):
    """`git add -A` first, because an untracked file is invisible to a plain
    `git diff` — and "the agent created a file and the PR did not contain it"
    is the kind of silent omission that makes a review meaningless."""
    (checkout / "brand_new.py").write_text("x = 1\n")
    assert "brand_new.py" in capture_patch(TEAM_A, REPO)


def test_proposing_nothing_is_refused(checkout):
    with pytest.raises(PullRequestError, match="nothing has changed"):
        capture_patch(TEAM_A, REPO)


def test_an_enormous_change_is_refused(checkout):
    """"Split it up" is the right answer to a 200KB diff from an agent. That it
    would also be an unpleasant jsonb column is the least of the reasons."""
    (checkout / "huge.py").write_text("x = 1\n" * (PATCH_MAX_CHARS // 4))
    with pytest.raises(PullRequestError, match="smaller change"):
        capture_patch(TEAM_A, REPO)


def test_an_unchecked_out_repo_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root", str(tmp_path / "ws")
    )
    with pytest.raises(PullRequestError, match="not checked out"):
        capture_patch(TEAM_A, REPO)


# ---------------------------------------------------------------------------
# Opening the pull request
# ---------------------------------------------------------------------------

@pytest.fixture
def no_github(monkeypatch):
    """Everything except the one call that must reach github.com."""
    calls = []

    def _fake(repo_full_name, head, base, title, body, token):
        calls.append({"head": head, "base": base, "title": title})
        return {"pr_url": f"https://github.com/{repo_full_name}/pull/1",
                "created": True}

    monkeypatch.setattr("pipeline.repo_pr._create_pr", _fake)
    return calls


def test_it_pushes_a_branch_and_opens_a_pr(checkout, origin, no_github):
    (checkout / "app.py").write_text("print('goodbye')\n")
    patch = capture_patch(TEAM_A, REPO)

    result = open_pull_request(
        TEAM_A, REPO, "Say goodbye", "because hello was wrong", patch, "abc123def456"
    )
    assert result["pr_url"].endswith("/pull/1")

    branches = _git(origin, "branch", "--list")
    assert "comrade/abc123def456" in branches


def test_it_never_pushes_to_the_default_branch(checkout, origin, no_github):
    """The constraint Copilot's coding agent enforces, and the reason review
    stays mandatory: the agent has no route to main at all."""
    (checkout / "app.py").write_text("print('goodbye')\n")
    patch = capture_patch(TEAM_A, REPO)
    open_pull_request(TEAM_A, REPO, "t", "b", patch, "abc123def456")

    main = _git(origin, "show", "main:app.py")
    assert main == "print('hello')\n"
    assert no_github[0]["base"] == "main"
    assert no_github[0]["head"].startswith(BRANCH_PREFIX)


def test_the_branch_comes_from_the_action_hash(checkout, origin, no_github):
    """Derived, not generated — this is what makes a retry converge instead of
    opening a second pull request."""
    assert branch_for("0123456789abcdef") == "comrade/0123456789ab"


def test_executing_twice_converges_on_one_branch(checkout, origin, no_github):
    """🔴 The property the CAS cannot provide.

    execute_consent claims the row and calls the executor inside one Postgres
    transaction, and this executor pushes to GitHub in the middle of it. A push
    that lands before a rollback leaves a branch the row says does not exist,
    and the retry runs the whole thing again.

    So the action is built to be repeatable rather than guaranteed-once: same
    hash, same branch, same result.
    """
    (checkout / "app.py").write_text("print('goodbye')\n")
    patch = capture_patch(TEAM_A, REPO)

    first = open_pull_request(TEAM_A, REPO, "t", "b", patch, "abc123def456")
    second = open_pull_request(TEAM_A, REPO, "t", "b", patch, "abc123def456")

    assert first["pr_url"] == second["pr_url"]
    branches = [b.strip(" *") for b in _git(origin, "branch", "--list").splitlines()]
    assert branches.count("comrade/abc123def456") == 1


def test_the_apply_starts_from_the_base_not_the_current_tree(
    checkout, origin, no_github
):
    """The patch is the authority: it is what a human read and approved.

    Starting from whatever the working tree holds at approve time would push
    changes nobody reviewed — an intervening turn's abandoned edits riding
    along inside an approved pull request.
    """
    (checkout / "app.py").write_text("print('goodbye')\n")
    patch = capture_patch(TEAM_A, REPO)

    # An unrelated, unreviewed edit made after the proposal.
    (checkout / "sneaky.py").write_text("exfiltrate()\n")

    open_pull_request(TEAM_A, REPO, "t", "b", patch, "abc123def456")
    files = _git(origin, "ls-tree", "-r", "--name-only", "comrade/abc123def456")
    assert "sneaky.py" not in files
    assert "app.py" in files


def test_a_patch_that_no_longer_applies_says_why(checkout, origin, no_github):
    """Somebody changed the same lines first. The member gets an explanation
    they can act on rather than a git error."""
    (checkout / "app.py").write_text("print('goodbye')\n")
    patch = capture_patch(TEAM_A, REPO)

    # The base moves underneath the approved change.
    seed = origin.parent / "seed"
    (seed / "app.py").write_text("print('something else entirely')\n")
    _git(seed, "commit", "-qam", "conflicting")
    _git(seed, "push", "-q", "origin", "main")

    with pytest.raises(PullRequestError, match="no longer applies"):
        open_pull_request(TEAM_A, REPO, "t", "b", patch, "abc123def456")


def test_the_default_branch_is_read_not_assumed(checkout):
    assert default_branch(TEAM_A, REPO) == "main"


# ---------------------------------------------------------------------------
# Through the consent protocol
# ---------------------------------------------------------------------------

def test_a_pr_is_a_T2_shared_and_reversible_action():
    """Not T1: it lands on the team's repository, not on one member."""
    from shared.consent import resolve_tier

    assert resolve_tier("repo_open_pr") == "T2"


def test_the_agent_may_name_this_one(seeded):
    """The third entry in AGENT_PROPOSABLE, and the only one of the three
    executors that exists specifically so the model can ask for it."""
    from shared.consent import AGENT_PROPOSABLE

    assert "repo_open_pr" in AGENT_PROPOSABLE


def test_a_proposal_naming_a_disconnected_repo_is_refused(seeded, checkout, admin):
    """Checked at EXECUTE time. A repository can be disconnected between the
    proposal and the approval, and pushing to one a team no longer claims is
    not something an old card should still be able to do."""
    from shared.consent import ConsentError, approve_consent, propose_action

    proposal = propose_action(
        team_id=TEAM_A, requester_id=_A1(), tool_name="repo_open_pr",
        args={"repo_full_name": "acme/gone", "title": "t", "body": "b",
              "patch": "diff --git a/x b/x\\n"},
    )
    with pytest.raises(ConsentError, match="no longer connected"):
        approve_consent(TEAM_A, proposal["consent_id"], _A1())


def test_a_proposal_with_no_change_never_reaches_git(seeded, admin):
    """The precheck, which is cheap, before the executor, which clones."""
    from shared.consent import ConsentError, approve_consent, propose_action

    admin.execute(
        "insert into public.github_repos (team_id, repo_full_name)"
        " values (%s,%s) on conflict do nothing",
        (TEAM_A, REPO),
    )
    proposal = propose_action(
        team_id=TEAM_A, requester_id=_A1(), tool_name="repo_open_pr",
        args={"repo_full_name": REPO, "title": "t", "body": "b", "patch": "   "},
    )
    with pytest.raises(ConsentError, match="no change"):
        approve_consent(TEAM_A, proposal["consent_id"], _A1())


def _A1() -> str:
    from tests._seed import A1

    return A1


def test_an_empty_repository_says_so_plainly(tmp_path, monkeypatch):
    """🔴 Found the first time this met a real repository.

    A brand-new GitHub repo has no commits, so no default branch. The old code
    guessed "main", and `git fetch origin main` then failed with "couldn't find
    remote ref main" — a message about the wrong thing entirely, surfacing from
    three functions away, for a situation with a perfectly clear explanation.

    A team connecting a repository they just created is not an exotic case, and
    GitHub cannot open a pull request against an empty repository either.
    """
    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root", str(tmp_path / "ws")
    )
    monkeypatch.setattr("shared.config.settings.github_pat", "unused-locally")

    empty = tmp_path / "empty.git"
    subprocess.run(["git", "init", "-q", "--bare", str(empty)],
                   check=True, capture_output=True)
    monkeypatch.setattr("pipeline.repo_sync._url_for", lambda _n: str(empty))
    from pipeline.repo_sync import sync_repo

    sync_repo(TEAM_A, REPO)
    with pytest.raises(PullRequestError, match="no commits yet"):
        default_branch(TEAM_A, REPO)


def test_the_default_branch_is_asked_of_the_remote_not_guessed(checkout, origin):
    """`ls-remote --symref` is authoritative and works on a shallow clone,
    where refs/remotes/origin/HEAD is not set locally — which is exactly the
    clone sync_repo makes."""
    assert default_branch(TEAM_A, REPO) == "main"
