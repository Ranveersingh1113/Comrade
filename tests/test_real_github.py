"""The lane that does not fake its dependencies.

Every other test drives git against a local bare repository through the
`_url_for` and `_create_pr` seams. Those seams exist so the logic can be tested
offline, and they are precisely where the expensive bugs hid — a seam that
makes testing easy is a place reality never gets checked:

  * `default_branch` fell back to the literal "main" and met a repository with
    no commits at all.
  * `git apply` was handed a patch through a TEXT pipe, so Python rewrote every
    newline to CRLF and the patch failed as "does not apply" — indistinguishable
    from a genuine conflict.
  * `--force-with-lease` refused with "stale info" because `fetch origin
    <branch>` writes only FETCH_HEAD and never the remote-tracking ref the
    lease reads.
  * `enqueue_sync` was never called, and the worker had no handler for the job
    it would have enqueued.

Not one of those was reachable from a local bare repo. This test would have
caught all four, which is why it is committed rather than rewritten as a
scratch file for the third time.

IT CLEANS UP AFTER ITSELF. The pull request it opens is closed and its branch
deleted in a finally, because a test that leaves a PR behind on every run is a
test somebody disables.

Marked `realgithub`, excluded by default: it is slow, needs the network, and
writes to a real repository. `scripts/gates.sh` runs it before a merge.
"""
import os
import uuid

import httpx
import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, TEAM_A

#: A scratch repository this credential can push to. Overridable, because
#: nobody else's checkout should assume mine.
REPO = os.environ.get("COMRADE_E2E_REPO", "Ranveersingh1113/test")

pytestmark = pytest.mark.realgithub


def _credentialled() -> bool:
    """Whether anything here can reach GitHub at all.

    The PAT path, deliberately: this test is about git and pull-request
    mechanics against a real remote. Minting an installation token is covered
    by test_github_app.py and by a live `GET /app` check, and claiming a real
    installation id for a seeded test team would collide with the unique
    constraint that makes installations unambiguous.
    """
    return bool(settings.github_pat)


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.execute("delete from public.github_repos where team_id = %s", (TEAM_A,))
        conn.close()


class Ctx:
    def __init__(self, repo):
        self.state = {
            "team_id": TEAM_A, "requester_id": A1, "repo_full_name": repo,
        }


def _close(pr_url: str, branch: str) -> None:
    """Close the pull request and delete its branch.

    Does not RAISE — a teardown that fails the test would report a cleanup
    problem as a product problem. But it does SAY SO. The first version
    swallowed the status code as well as the exception, and left an orphaned
    branch on the repository that nothing anywhere mentioned; httpx does not
    raise on a 4xx, so "silently failed" and "worked" looked identical. That is
    the same silent-failure shape as rmtree(ignore_errors=True) and
    `pytest | tail && commit`, in a third place.
    """
    headers = {
        "Authorization": f"Bearer {settings.github_pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    number = pr_url.rsplit("/", 1)[-1]
    for what, call in (
        ("close the pull request", lambda: httpx.patch(
            f"https://api.github.com/repos/{REPO}/pulls/{number}",
            headers=headers, json={"state": "closed"}, timeout=30)),
        ("delete the branch", lambda: httpx.delete(
            f"https://api.github.com/repos/{REPO}/git/refs/heads/{branch}",
            headers=headers, timeout=30)),
    ):
        if what == "delete the branch" and not branch:
            print(f"[cleanup] no branch recorded; {REPO} may have an orphan")
            continue
        try:
            resp = call()
        except httpx.HTTPError as exc:
            print(f"[cleanup] could not {what}: {exc}")
            continue
        if resp.status_code >= 400:
            print(f"[cleanup] could not {what}: {resp.status_code}"
                  f" {resp.text[:120]}")


@pytest.mark.skipif(not _credentialled(), reason="no GitHub credential configured")
def test_the_whole_path_against_a_real_repository(seeded, admin, monkeypatch):
    """Connect, clone, read, edit, run, propose, approve, pull request.

    Through the code the product runs — worker.tick() rather than the sweep
    function, approve_consent rather than the executor — because every bug this
    replaces lived between two pieces that each worked.
    """
    from agent.repo_tools import (
        connected_repo, repo_edit, repo_propose_pr, repo_read, repo_run,
    )
    from pipeline.repo_pr import branch_for
    from pipeline.worker import run_once, tick
    from shared.consent import approve_consent

    # The single-tenant PAT guard counts teams that would fall through to the
    # PAT. A developer's own App-connected repository does not, so this stays
    # honest without touching their rows.
    admin.execute(
        "insert into public.github_repos (team_id, repo_full_name)"
        " values (%s,%s) on conflict do nothing",
        (TEAM_A, REPO),
    )

    # 1. Connected, and NOT yet visible: last_cloned_at is null until a clone.
    assert connected_repo(TEAM_A, A1) is None

    # 2. The worker clones it. Through tick(), because the bug was that nothing
    #    called the sweep and nothing registered the handler.
    tick()
    for _ in range(20):
        if not run_once():
            break
    assert connected_repo(TEAM_A, A1) == REPO, (
        "the worker never cloned it — check that main() imports the module"
        " registering the sync_repo handler"
    )

    ctx = Ctx(REPO)

    # 3. Read something that is really there.
    readme = repo_read("README.md", ctx)
    assert readme.get("text"), readme
    assert "^" in readme["text"], "repository content must reach the model datamarked"

    # 4. Write, and check the write by running it — contained.
    stamp = uuid.uuid4().hex[:8]
    name = f"comrade_check_{stamp}.py"
    edit = repo_edit(
        name, "",
        f'VALUE = "{stamp}"\n\n'
        f'if __name__ == "__main__":\n'
        f'    assert VALUE == "{stamp}"\n'
        f'    print("ok")\n',
        ctx,
    )
    assert edit.get("created"), edit
    ran = repo_run(f"python {name}", ctx)
    assert ran.get("exit_code") == 0, ran
    assert "ok" in ran["stdout"].replace("^", " ")

    # 5. Propose. The card carries the literal diff, not a pointer.
    proposal = repo_propose_pr(
        f"Comrade gate check {stamp}",
        "Opened by tests/test_real_github.py and closed again immediately.",
        ctx,
    )
    assert proposal.get("status") == "pending", proposal

    # The hash the branch name is derived from. Read from the row rather than
    # returned by the tool: the agent has no business knowing it, and adding it
    # to the tool's result purely so a test could assert on it would widen the
    # surface for the test's convenience.
    action_hash = admin.execute(
        "select action_hash from public.consent_queue where id = %s",
        (proposal["consent_id"],),
    ).fetchone()[0]

    # 6. Approve, which pushes and opens the pull request for real.
    pr_url = None
    branch = None
    try:
        outcome = approve_consent(TEAM_A, proposal["consent_id"], A1)
        assert outcome["status"] == "executed", outcome
        pr_url = outcome["result"]["pr_url"]
        assert f"/{REPO}/pull/" in pr_url, pr_url

        number = pr_url.rsplit("/", 1)[-1]
        auth = {"Authorization": f"Bearer {settings.github_pat}",
                "Accept": "application/vnd.github+json"}

        body = httpx.get(f"https://api.github.com/repos/{REPO}/pulls/{number}",
                         headers=auth, timeout=30).json()
        branch = body["head"]["ref"]
        # DERIVED from the action hash, never generated — that is what makes a
        # retried execute find its own pull request instead of opening a
        # second one. Asserted against branch_for's actual output, because
        # `startswith("comrade/")` alone would pass for a randomly generated
        # name carrying the right prefix, which is the whole property this is
        # supposed to be checking.
        assert branch == branch_for(action_hash), (
            f"{branch} is not the branch derived from this action's hash;"
            " a retry would open a second pull request"
        )
        assert body["base"]["ref"] != branch, "a PR must never target its own branch"

        files = httpx.get(
            f"https://api.github.com/repos/{REPO}/pulls/{number}/files",
            headers=auth, timeout=30,
        ).json()
        assert [f["filename"] for f in files] == [name], (
            "the pull request must contain exactly the edit and nothing else"
        )
    finally:
        if pr_url:
            _close(pr_url, branch or "")


@pytest.mark.skipif(not _credentialled(), reason="no GitHub credential configured")
def test_an_empty_repository_is_named_as_empty(seeded, admin, monkeypatch, tmp_path):
    """🔴 The failure the first real run met, and the local tests could not.

    A brand-new GitHub repository has no commits, so no default branch.
    `default_branch` guessed "main", and `git fetch origin main` then failed
    with "couldn't find remote ref main" — a message about the wrong thing
    entirely, arriving from three functions away.
    """
    from pipeline.repo_sync import RepoSyncError, default_branch

    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root", str(tmp_path / "ws")
    )
    empty = tmp_path / "empty.git"
    import subprocess

    subprocess.run(["git", "init", "-q", "--bare", str(empty)],
                   check=True, capture_output=True)
    monkeypatch.setattr("pipeline.repo_sync._url_for", lambda _n: str(empty))
    from pipeline.repo_sync import sync_repo

    sync_repo(TEAM_A, REPO)
    with pytest.raises(RepoSyncError, match="no commits yet"):
        default_branch(TEAM_A, REPO)
