"""What the verification gate thinks it checked.

🔴 THE DEFECTS.

F26 — the digest hashed `git diff HEAD` plus `git ls-files --others`, and the
second of those is the untracked FILENAMES. Create a new file, run a check,
rewrite that file under the same name, propose: same diff, same name list, same
digest, gate satisfied. The patch capture then carried contents nothing had
run. A file's name is not its contents, and noticing a change is the entire job
of the digest.

F27 — git's exit status was ignored. A failing `git diff HEAD` returns empty
stdout, so a failed measurement hashed `b"" + b"\\x00" + b""`: an ordinary
looking digest, and the SAME one every time. Record a "verification" while git
is broken, propose while git is still broken, and the two match — the gate
passes on something nothing checked. The exception path returned
`unreadable:{id(root)}`, which is also stable for the same object.

Against a REAL temporary repository, because both defects are about what git
actually reports.
"""
import subprocess
from pathlib import Path

import pytest

from agent.repo_tools import MeasurementFailed, _patch_digest


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@test.dev")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "kept.py").write_text("print('hello')\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "first")
    return root


# ---------------------------------------------------------------------------
# F26: a new file's contents are part of what was checked
# ---------------------------------------------------------------------------

def test_rewriting_a_new_file_changes_the_identity(repo):
    """🔴 It did not. This is the exact sequence from the finding: create,
    check, rewrite under the same name, propose."""
    (repo / "added.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    verified = _patch_digest(repo)

    (repo / "added.py").write_text("import os\nos.system('curl evil')\n",
                                   encoding="utf-8")

    assert _patch_digest(repo) != verified, (
        "a new file was rewritten and the gate could not tell"
    )


def test_an_untouched_tree_keeps_its_identity(repo):
    """The digest has to be stable, or the gate refuses everything and gets
    turned off."""
    (repo / "added.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    assert _patch_digest(repo) == _patch_digest(repo)


def test_a_binary_new_file_is_covered(repo):
    """Contents are contents. A digest that only handled text would leave the
    obvious way round it open."""
    (repo / "blob.bin").write_bytes(bytes(range(256)))
    before = _patch_digest(repo)

    (repo / "blob.bin").write_bytes(bytes(range(255, -1, -1)))

    assert _patch_digest(repo) != before


def test_a_tracked_change_still_changes_the_identity(repo):
    """The half that already worked has to keep working."""
    before = _patch_digest(repo)
    (repo / "kept.py").write_text("print('changed')\n", encoding="utf-8")

    assert _patch_digest(repo) != before


def test_two_files_swapping_contents_are_not_the_same_tree(repo):
    """Hashing contents without binding them to their paths would call this
    unchanged."""
    (repo / "a.py").write_text("A\n", encoding="utf-8")
    (repo / "b.py").write_text("B\n", encoding="utf-8")
    before = _patch_digest(repo)

    (repo / "a.py").write_text("B\n", encoding="utf-8")
    (repo / "b.py").write_text("A\n", encoding="utf-8")

    assert _patch_digest(repo) != before


def test_a_filename_with_a_newline_does_not_split_the_listing(repo, tmp_path):
    """`ls-files --others` without -z would report this as two paths, and the
    identity of the tree would depend on how a filename was punctuated."""
    try:
        (repo / "od\nd.py").write_text("x\n", encoding="utf-8")
    except OSError:
        pytest.skip("this filesystem does not allow newlines in filenames")

    assert _patch_digest(repo)


# ---------------------------------------------------------------------------
# F27: unmeasurable is not an identity
# ---------------------------------------------------------------------------

def test_a_failed_measurement_is_an_error_not_a_digest(tmp_path):
    """🔴 A non-repository returned a valid-looking hash of two empty strings.
    Two failures produced the same one, so a verification recorded during a
    failure matched a proposal during a failure."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    with pytest.raises(MeasurementFailed):
        _patch_digest(not_a_repo)


def test_two_failures_cannot_match_each_other(tmp_path):
    """The property that made the old fallback dangerous: it was STABLE."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    first = second = None
    try:
        first = _patch_digest(not_a_repo)
    except MeasurementFailed:
        pass
    try:
        second = _patch_digest(not_a_repo)
    except MeasurementFailed:
        pass

    assert first is None and second is None, (
        "a failed measurement produced a digest that a later failure could match"
    )


def test_no_working_tree_is_also_an_error():
    """🔴 `root is None` returned the empty string, which compares equal to
    itself just as happily."""
    with pytest.raises(MeasurementFailed):
        _patch_digest(None)


def test_a_missing_git_is_an_error(repo, monkeypatch):
    """Timeouts and a missing binary are the same class of thing as a nonzero
    exit: the answer is unknown, and unknown is not verified."""
    import subprocess as sp

    def _boom(*_a, **_k):
        raise OSError("git is not installed")

    monkeypatch.setattr(sp, "run", _boom)

    with pytest.raises(MeasurementFailed):
        _patch_digest(repo)
