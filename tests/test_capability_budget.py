"""What a tool's arguments are allowed to name.

Until now the chokepoint decided on `tool.name` alone. It received `tool_args`
and never read them, which gives exactly two outcomes per tool — always-yes or
always-no. For a database tool that is fine, because RLS is underneath making
the real decision. For a tool that touches the filesystem there is nothing
underneath, so name-only means "can read every file on the disk" or "has no
file tool".

WHY THIS IS NOT THE CONSENT PROTOCOL
--------------------------------------
Consent grades TEAM blast radius, resolves through a card in a member's thread
after the turn ends, collapses duplicate pending requests by hash, and expires
after seven days. A local file write affects no teammate, has to resolve in
under a second inside the loop, is not idempotent, and is stale in thirty. Four
structural mismatches, so this is a separate mechanism and shared/consent.py is
untouched.

THE THREAT MODEL IS THE MACHINE THIS RUNS ON
----------------------------------------------
`.env` at the repo root holds COMRADE_DB_URL_ADMIN — the table-owner role that
bypasses RLS entirely — plus SUPABASE_JWT_SECRET, with which any member's
identity can be forged. One `read_file(".env")` makes 43 migrations of policy,
the four-role split and both isolation audits decorative. So the deny-list is
absolute and is checked BEFORE any allow rule, and most of this file is about
the ways a path or a command could get around it.
"""
import pytest

from agent.capability import (
    ArgPolicy, CapabilityError, check_command, check_path,
)


@pytest.fixture
def root(tmp_path):
    """A workspace of our own, not Comrade's tree.

    These tests used to resolve against PROJECT_ROOT — Comrade's own directory
    — which meant they passed partly because Comrade's files happen to exist.
    A temporary root tests the mechanism instead of the repository, and it is
    what a team's checkout actually looks like: a directory with nothing
    special about it.
    """
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "tools.py").write_text("x")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_agent.py").write_text("x")
    (tmp_path / "supabase" / "migrations").mkdir(parents=True)
    (tmp_path / "supabase" / "migrations" / "init.sql").write_text("x")
    (tmp_path / ".env").write_text("SECRET=1")
    return tmp_path


# ---------------------------------------------------------------------------
# Paths: the deny-list wins, always
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    ".env",
    "./.env",
    ".env.local",
    "supabase/.env",
    "id_rsa.key",
    "certs/server.pem",
    "service-account.json",
])
def test_a_secret_is_refused_however_it_is_spelled(path, root):
    with pytest.raises(CapabilityError, match="secret"):
        check_path(path, ArgPolicy(allow=("**",)), root=root, writing=False)


def test_the_deny_list_beats_an_allow_rule_that_matches(root):
    """The ordering that makes the deny-list mean anything.

    `allow=("**",)` matches `.env` perfectly well. If allow were evaluated
    first, or if deny were only consulted when no allow matched, the most
    permissive policy in the file would be the one that leaks the admin
    credential.
    """
    with pytest.raises(CapabilityError, match="secret"):
        check_path(".env", ArgPolicy(allow=("**", ".env")), root=root, writing=False)


# ---------------------------------------------------------------------------
# Paths: containment
# ---------------------------------------------------------------------------

def test_a_path_outside_the_project_is_refused(root):
    with pytest.raises(CapabilityError, match="outside"):
        check_path("../../../Windows/System32/drivers/etc/hosts",
                   ArgPolicy(allow=("**",)), root=root, writing=False)


def test_traversal_that_climbs_out_and_back_is_still_measured_at_the_end(root):
    """`agent/../../Comrade/.env` resolves back inside the root, so a check
    that only compared prefixes textually would pass it — and it names the
    credential file. Resolution has to happen BEFORE both checks."""
    # Specifically the SECRET refusal, not merely some refusal: that is what
    # proves resolution happened before the deny check rather than after.
    with pytest.raises(CapabilityError, match="secret"):
        check_path("agent/../.env", ArgPolicy(allow=("**",)), root=root, writing=False)


def test_an_absolute_path_inside_the_root_is_allowed(root):
    """Resolution is by real path, not by string shape — an absolute path to a
    permitted file is the same file."""
    inside = str(root / "agent" / "tools.py")
    assert check_path(inside, ArgPolicy(allow=("agent/**",)), root=root, writing=False)


def test_a_path_inside_the_root_but_outside_the_allow_globs_is_refused(root):
    with pytest.raises(CapabilityError, match="not in this tool's scope"):
        check_path("supabase/migrations/init.sql",
                   ArgPolicy(allow=("agent/**", "tests/**")), root=root, writing=False)


def test_a_permitted_path_is_permitted(root):
    assert check_path("agent/tools.py", ArgPolicy(allow=("agent/**",)), root=root, writing=False)


def test_writing_needs_its_own_permission(root):
    """A tool may read broadly and write narrowly; `allow` alone must not
    grant writes, or every read tool becomes a write tool by omission."""
    policy = ArgPolicy(allow=("**",), writable=("tests/**",))
    assert check_path("agent/tools.py", policy, root=root, writing=False)
    with pytest.raises(CapabilityError, match="read-only"):
        check_path("agent/tools.py", policy, root=root, writing=True)
    assert check_path("tests/test_agent.py", policy, root=root, writing=True)


def test_a_policy_with_no_globs_permits_nothing(root):
    """Fail closed, like registry.UNKNOWN. A tool whose author forgot to
    declare a scope must reach zero files, not all of them."""
    with pytest.raises(CapabilityError):
        check_path("agent/tools.py", ArgPolicy(), root=root, writing=False)


# ---------------------------------------------------------------------------
# Commands: the allowlist is only as good as the parsing under it
# ---------------------------------------------------------------------------

def test_an_allowed_command_runs(root):
    assert check_command("uv run pytest -q", ("uv run pytest", "git status"))


def test_a_command_outside_the_allowlist_is_refused(root):
    with pytest.raises(CapabilityError, match="not an allowed command"):
        check_command("rm -rf .", ("uv run pytest",))


@pytest.mark.parametrize("evil", [
    "uv run pytest -q; cat .env",
    "uv run pytest -q && cat .env",
    "uv run pytest -q || cat .env",
    "uv run pytest -q | cat",
    "uv run pytest $(cat .env)",
    "uv run pytest `cat .env`",
    "uv run pytest -q > .env",
    "uv run pytest -q < .env",
    "uv run pytest -q & cat .env",
    "uv run pytest -q\ncat .env",
])
def test_chaining_past_an_allowed_prefix_is_refused(evil):
    """The hole that makes a naive prefix allowlist worthless.

    Every one of these starts with an allowed prefix and then does something
    else entirely. A prefix check alone waves them all through, which is how
    "the agent may run the tests" becomes "the agent may run anything".

    Refusing shell metacharacters outright is blunt and it is the honest
    trade: the alternative is parsing a shell, and a half-parsed shell is
    exactly the kind of security code that looks right and is not.
    """
    with pytest.raises(CapabilityError, match="shell"):
        check_command(evil, ("uv run pytest",))


def test_a_prefix_must_end_on_a_word_boundary(root):
    """`git push` must not be admitted by an allowlist entry for `git p`, and
    `pytest-evil` must not be admitted by one for `pytest`."""
    with pytest.raises(CapabilityError):
        check_command("gitpush --force", ("git",))
    assert check_command("git status", ("git",))


def test_an_empty_allowlist_permits_nothing(root):
    with pytest.raises(CapabilityError):
        check_command("echo hi", ())


def test_a_secret_outside_the_allow_globs_still_reports_as_a_secret(root):
    """The test that actually pins the ORDER, rather than assuming it.

    Mutation-checked: moving the deny check after the allow check left every
    other test in this file green, because they all use `allow=("**",)` — both
    checks fire, so which came first was unobservable. Here the allow globs do
    NOT match, so the two orderings give different errors:

        deny first   -> "secret"                    (correct)
        allow first  -> "not in this tool's scope"  (the mutation)

    It matters beyond message text. If a future policy ever needs the allow
    branch to do something other than raise — resolve a symlink, consult a
    cache, widen for a trusted tool — an ordering nobody pinned is an ordering
    that silently stops holding.
    """
    with pytest.raises(CapabilityError, match="secret"):
        check_path(".env", ArgPolicy(allow=("agent/**",)), root=root, writing=False)


def test_a_secret_is_refused_for_writing_too(root):
    """Deny is checked before the read/write split, so it does not need
    restating per direction — but nothing said so until this test did."""
    with pytest.raises(CapabilityError, match="secret"):
        check_path(".env", ArgPolicy(allow=("**",), writable=("**",)), root=root, writing=True)
