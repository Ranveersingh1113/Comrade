"""The agent can change a working copy. Nothing reaches the team yet.

Phase C1. `repo_edit` writes into the team's checkout — a scratch tree only
this turn can see. The reviewable action is the pull request (C2), gated by the
consent queue; asking for approval per file would put a card in a member's
thread for every step of one change, which is the consent fatigue §5 exists to
avoid.

REPLACEMENT, NOT REWRITING
----------------------------
Most of this file is about `old_text` having to match EXACTLY ONCE. A tool that
takes a whole file back from a model silently loses whatever the model did not
think to reproduce, and a file is nearly always longer than the part anyone
means to change. Zero matches means the file is not what the model thinks;
several means the edit is ambiguous. Both are refusals, because guessing which
occurrence was meant is how an agent quietly changes the wrong line.
"""
from types import SimpleNamespace

import pytest

from agent.repo_tools import repo_edit, repo_read
from shared.workspace import repo_checkout

TEAM_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TEAM_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
REPO = "acme/app"


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root", str(tmp_path / "ws")
    )
    root = repo_checkout(TEAM_A, REPO)
    (root / "src").mkdir(parents=True)
    (root / "src" / "auth.py").write_text(
        "def authenticate(user):\n    return True\n"
    )
    (root / "src" / "dup.py").write_text("x = 1\nx = 1\n")
    (root / ".env").write_text("SECRET=1\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[remote]\n")
    return root


@pytest.fixture
def ctx():
    return SimpleNamespace(state={"team_id": TEAM_A, "repo_full_name": REPO})


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------

def test_it_replaces_an_exact_passage(checkout, ctx):
    result = repo_edit("src/auth.py", "return True", "return verify(user)", ctx)
    assert result["replaced"] is True
    assert (checkout / "src" / "auth.py").read_text() == (
        "def authenticate(user):\n    return verify(user)\n"
    )


def test_it_leaves_the_rest_of_the_file_alone(checkout, ctx):
    """The reason this is replacement rather than rewriting: a model handing
    back a whole file loses whatever it did not think to reproduce."""
    repo_edit("src/auth.py", "return True", "return False", ctx)
    assert "def authenticate(user):" in (checkout / "src" / "auth.py").read_text()


def test_text_that_appears_twice_is_refused_not_guessed(checkout, ctx):
    """Guessing which occurrence was meant is how an agent quietly changes the
    wrong line — and the wrong line in a diff a human is about to approve."""
    result = repo_edit("src/dup.py", "x = 1", "x = 2", ctx)
    assert "ambiguous" in result["error"]
    assert (checkout / "src" / "dup.py").read_text() == "x = 1\nx = 1\n"


def test_text_that_is_absent_is_refused(checkout, ctx):
    """Not "no-op": the file is not what the model believes it is, and trying
    a different phrasing against a wrong belief is worse than stopping."""
    result = repo_edit("src/auth.py", "return Maybe", "return False", ctx)
    assert "does not appear" in result["error"]


def test_a_new_file_is_created_with_empty_old_text(checkout, ctx):
    result = repo_edit("src/new.py", "", "print('hi')\n", ctx)
    assert result["created"] is True
    assert (checkout / "src" / "new.py").read_text() == "print('hi')\n"


def test_creating_over_an_existing_file_is_refused(checkout, ctx):
    """Otherwise "create" is a silent whole-file overwrite by another name."""
    result = repo_edit("src/auth.py", "", "print('clobbered')\n", ctx)
    assert "already exists" in result["error"]
    assert "authenticate" in (checkout / "src" / "auth.py").read_text()


def test_a_new_file_in_a_new_directory_works(checkout, ctx):
    assert repo_edit("src/deep/nested/x.py", "", "y = 1\n", ctx)["created"] is True


def test_an_edit_is_visible_to_the_read_tool(checkout, ctx):
    """The loop this exists for: edit, then read back to check."""
    repo_edit("src/auth.py", "return True", "return verify(user)", ctx)
    assert "verify" in repo_read("src/auth.py", ctx)["text"].replace("^", " ")


# ---------------------------------------------------------------------------
# The boundary is the same one reading has
# ---------------------------------------------------------------------------

def test_a_secret_cannot_be_edited(checkout, ctx):
    result = repo_edit(".env", "SECRET=1", "SECRET=stolen", ctx)
    assert "secret" in result["error"]
    assert (checkout / ".env").read_text() == "SECRET=1\n"


def test_git_cannot_be_edited(checkout, ctx):
    """History is how a change gets reviewed and reverted. An agent that can
    rewrite it can erase what it did."""
    result = repo_edit(".git/config", "[remote]", "[evil]", ctx)
    assert "error" in result
    assert (checkout / ".git" / "config").read_text() == "[remote]\n"


def test_another_teams_checkout_cannot_be_edited(checkout, ctx):
    other = repo_checkout(TEAM_B, REPO)
    other.mkdir(parents=True)
    (other / "app.py").write_text("team B\n")
    result = repo_edit(f"../../{TEAM_B}/acme__app/app.py", "team B", "hacked", ctx)
    assert "error" in result
    assert (other / "app.py").read_text() == "team B\n"


def test_a_turn_with_no_repo_edits_nothing(checkout):
    no_repo = SimpleNamespace(state={"team_id": TEAM_A})
    assert "error" in repo_edit("src/auth.py", "a", "b", no_repo)


# ---------------------------------------------------------------------------
# The write cap lives in the chokepoint, and this is what arms it
# ---------------------------------------------------------------------------

def test_the_tool_is_declared_as_writing(checkout):
    """`writes=True` is what makes the chokepoint count this call against the
    per-turn cap. Declared read-only, the tool would still write files and
    nothing would be counting — the bound on this tool is not "may it write"
    but "how much"."""
    from agent.registry import spec_for

    spec = spec_for("repo_edit")
    assert spec.surface == "sandbox"
    assert spec.writes is True
    assert spec.args.writable == ("**",)
    assert spec.args.max_writes_per_turn == 20


def test_editing_does_not_ask_a_member_per_file(checkout):
    """A card per file for one change is the consent fatigue §5 exists to
    avoid. The reviewable action is the pull request, not the keystroke."""
    from agent.registry import spec_for

    assert spec_for("repo_edit").needs_human is False
