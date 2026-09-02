"""Turning a working copy into a pull request a human approves.

Phase C2. `repo_edit` changes a scratch tree; this is how those changes reach
the team — as a branch and a PR, never a push to a default branch. That is the
shape Copilot's coding agent and Cursor's cloud agents both enforce, and the
reason is the same: review is not optional if the author is a model.

WHY THE PATCH IS CAPTURED AT PROPOSE TIME
-------------------------------------------
The obvious design is "approve, then go and push whatever is in the checkout".
It is wrong, and the bug is not subtle once seen: `sync_repo` starts every turn
with `reset --hard` and `clean -fd`. A member who approves an hour later would
be approving a tree that some intervening turn had already wiped, and the
executor would push nothing at all — silently, because an empty diff is not an
error anywhere.

So the diff is captured when the proposal is made and stored in the consent
row. Two things fall out of that, both good:

  * The card carries the literal change, which is what the consent protocol
    promises everywhere else — "what you approve is exactly what runs" is a
    stronger claim when the row holds the patch rather than a pointer to a
    directory.
  * `action_hash` covers the patch, so editing the proposal re-stamps it and
    approving a stale one cannot apply a diff nobody read.

IDEMPOTENT BY CONSTRUCTION, BECAUSE THE CAS CANNOT REACH HERE
---------------------------------------------------------------
`execute_consent` claims the row and runs the executor inside one Postgres
transaction. This executor does network I/O in the middle of that, which is the
one place the exactly-once guarantee cannot cover: if the push succeeds and the
transaction then rolls back, the branch exists and the row says it does not.

The answer is not a cleverer transaction, it is an action that is safe to
repeat. The branch name is derived from `action_hash`, so a second attempt
finds its own branch and its own open PR and returns them rather than opening a
second one.
"""
import logging
import subprocess
from pathlib import Path

import httpx

from pipeline.repo_sync import (
    GIT_TIMEOUT_SECONDS, RepoSyncError, _auth_header, _token_for, _run_git,
)
from shared.workspace import repo_checkout

logger = logging.getLogger(__name__)

#: A diff bigger than this is not reviewable in one sitting, and storing it in
#: a jsonb column is the least of the reasons to refuse. "Split it up" is the
#: right answer to a 200KB change from an agent, not "store it anyway".
PATCH_MAX_CHARS = 100_000

BRANCH_PREFIX = "comrade/"
GITHUB_API = "https://api.github.com"


class PullRequestError(Exception):
    """A pull request could not be opened."""


def branch_for(action_hash: str) -> str:
    """The branch name for one approved action.

    Derived, not generated: this is what makes a retried execute find its own
    branch instead of opening a second PR. See the module header.
    """
    return f"{BRANCH_PREFIX}{action_hash[:12]}"


def _git_out(args: list[str], cwd: Path) -> str:
    """A read-only git command whose stdout we want. No credential needed."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, never a shell string
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise PullRequestError((proc.stderr or proc.stdout or "").strip()[:800])
    return proc.stdout


def capture_patch(team_id: str, repo_full_name: str) -> str:
    """Everything the agent changed in this checkout, as a patch.

    `add -A` first so new files appear: an untracked file is invisible to a
    plain `git diff`, and "the agent created a file and the PR did not contain
    it" is the kind of silent omission that makes a review meaningless.
    """
    checkout = repo_checkout(team_id, repo_full_name)
    if not (checkout / ".git").exists():
        raise PullRequestError(
            "this team's repository is not checked out, so there is nothing to"
            " propose."
        )
    _git_out(["add", "-A"], checkout)
    patch = _git_out(["diff", "--cached"], checkout)
    if not patch.strip():
        raise PullRequestError("nothing has changed, so there is nothing to open.")
    if len(patch) > PATCH_MAX_CHARS:
        raise PullRequestError(
            f"the change is {len(patch)} characters, past the"
            f" {PATCH_MAX_CHARS} a single pull request should carry. Make a"
            " smaller change and propose it on its own."
        )
    return patch


def default_branch(team_id: str, repo_full_name: str) -> str:
    """What the PR should target. Read from the clone, not assumed to be main."""
    checkout = repo_checkout(team_id, repo_full_name)
    try:
        ref = _git_out(
            ["symbolic-ref", "refs/remotes/origin/HEAD"], checkout
        ).strip()
        return ref.rsplit("/", 1)[-1]
    except PullRequestError:
        # A shallow clone of a repo with no remote HEAD set. Falling back is
        # better than failing: the PR API rejects a bad base loudly.
        return "main"


def _create_pr(
    repo_full_name: str, head: str, base: str, title: str, body: str, token: str
) -> dict:
    """Open the PR, or hand back the one already open for this branch.

    A seam, like repo_sync._url_for — the git half of this module is tested
    against a local bare repository, and this is the only part that must talk
    to github.com.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"{GITHUB_API}/repos/{repo_full_name}/pulls"
    resp = httpx.post(
        url,
        headers=headers,
        json={"title": title, "body": body, "head": head, "base": base},
        timeout=30.0,
    )
    if resp.status_code == 201:
        data = resp.json()
        return {"pr_url": data["html_url"], "created": True}

    # 422 with "already exists" is the retry path, not a failure: the branch is
    # pushed and a PR is open, which is exactly the state we were trying to
    # reach. See the module header on why this executor must be repeatable.
    if resp.status_code == 422 and "already exist" in resp.text.lower():
        existing = httpx.get(
            url, headers=headers,
            params={"head": f"{repo_full_name.split('/')[0]}:{head}",
                    "state": "open"},
            timeout=30.0,
        )
        items = existing.json() if existing.status_code == 200 else []
        if items:
            return {"pr_url": items[0]["html_url"], "created": False}
    raise PullRequestError(
        f"GitHub refused the pull request ({resp.status_code}):"
        f" {resp.text[:300]}"
    )


def open_pull_request(
    team_id: str,
    repo_full_name: str,
    title: str,
    body: str,
    patch: str,
    action_hash: str,
) -> dict:
    """Apply the approved patch to a fresh branch, push it, open the PR.

    Every step is safe to repeat. The branch is derived from action_hash, the
    apply starts from a clean checkout of the base, and a PR that already
    exists for the branch is returned rather than duplicated.
    """
    checkout = repo_checkout(team_id, repo_full_name)
    token = _token_for(team_id, repo_full_name)
    branch = branch_for(action_hash)
    base = default_branch(team_id, repo_full_name)

    try:
        # FETCH the base before resetting to it. The remote-tracking ref in
        # this checkout is as old as the last sync, and an approval can arrive
        # hours later — without this the patch is applied to a stale base, so
        # a change that genuinely conflicts with current main applies cleanly
        # here and the conflict surfaces to a human reviewer instead. Caught by
        # the test that moves the base underneath an approved patch, which
        # passed for the wrong reason until this was added.
        _run_git(["fetch", "origin", base], checkout, token)
        _run_git(["reset", "--hard", "FETCH_HEAD"], checkout, token)
        _run_git(["clean", "-fd"], checkout, token)
        _git_out(["checkout", "-B", branch], checkout)

        # BYTES on stdin, not text. With text=True Python wraps stdin in a
        # TextIOWrapper whose default newline handling rewrites newlines to
        # os.linesep — on Windows that turns every line of the patch into CRLF
        # and `git apply` rejects the whole thing. It fails as "patch does not
        # apply", which reads exactly like a genuine conflict and sent me
        # looking at the wrong half of this function.
        proc = subprocess.run(  # noqa: S603
            ["git", "apply", "--index", "-"],
            cwd=str(checkout), input=patch.encode("utf-8"),
            capture_output=True, timeout=GIT_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace")[:300]
            raise PullRequestError(
                "the approved change no longer applies to the current code —"
                " somebody changed the same lines first. Ask again and the"
                f" change will be rebuilt from what is there now. {detail}"
            )

        _git_out(
            ["-c", "user.email=comrade@users.noreply.github.com",
             "-c", "user.name=Comrade",
             "commit", "-m", title],
            checkout,
        )
        # Fetch our own branch before pushing over it. A retry pushes a
        # commit with the same TREE but a different hash (the timestamp
        # differs), so the push has to overwrite — and --force-with-lease
        # refuses with "stale info" when there is no local ref to lease
        # against, which is exactly the state a retry is in. Without the
        # fetch the second attempt fails and the idempotency this executor
        # depends on does not exist.
        #
        # --force-with-lease and not --force: the branch name embeds a hash of
        # the action, so the only realistic occupant is our own earlier
        # attempt, but leaning on that rather than checking it is how a force
        # push eventually eats somebody's work.
        # An EXPLICIT REFSPEC, not `fetch origin <branch>`. The short form
        # writes only FETCH_HEAD and leaves refs/remotes/origin/<branch>
        # missing — so --force-with-lease still has nothing to lease against
        # and still refuses with "stale info". The refspec is what updates the
        # remote-tracking ref the lease actually reads.
        subprocess.run(  # noqa: S603 - a missing branch is not an error here
            ["git", "-c", _auth_header(token), "fetch", "origin",
             f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
            cwd=str(checkout), capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        # The EXPLICIT lease form, `--force-with-lease=<ref>:<expected>`.
        # The bare `--force-with-lease` is documented as unreliable when a
        # fetch happens in the same script — it keeps refusing with "stale
        # info" even once the remote-tracking ref is current, which is exactly
        # the position a retry is in. Naming the sha we just fetched says
        # precisely what we expect to be overwriting, and an empty expected
        # value means "expect this branch not to exist yet".
        remote_sha = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--verify", "--quiet",
             f"refs/remotes/origin/{branch}"],
            cwd=str(checkout), capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        ).stdout.strip()
        _run_git(
            ["push", f"--force-with-lease={branch}:{remote_sha}", "origin", branch],
            checkout, token,
        )
    except (RepoSyncError, subprocess.TimeoutExpired) as exc:
        raise PullRequestError(str(exc)) from exc

    return _create_pr(repo_full_name, branch, base, title, body, token)
