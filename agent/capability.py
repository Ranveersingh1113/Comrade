"""What a tool's arguments are allowed to name.

`agent/registry.py` says WHICH tools exist and whether a human must see them.
This says what those tools may be pointed AT — the layer the chokepoint was
missing, because it received `tool_args` and decided on `tool.name` alone.

For a `db` tool that was fine: RLS is underneath making the real decision, and
the chokepoint is defence in depth above it. For a tool that touches the
filesystem there is nothing underneath. Name-only permission there means "can
read every file on the disk" or "has no file tool", and neither is usable.

WHY NOT THE CONSENT PROTOCOL
------------------------------
shared/consent.py grades TEAM blast radius, resolves through a card in a
member's thread after the turn ends, collapses duplicate pending requests by
action_hash, and expires after seven days. A local file write affects no
teammate, must resolve inside the loop in under a second, is not idempotent,
and is stale in thirty seconds. Four structural mismatches; this is a separate
mechanism and consent.py is untouched.

THE THREAT MODEL IS THIS MACHINE
----------------------------------
`.env` at the repo root holds COMRADE_DB_URL_ADMIN — the table-owner role that
bypasses RLS entirely — and SUPABASE_JWT_SECRET, with which any member's
identity can be forged. One read of that file makes 43 migrations of policy,
the four-role split and both isolation audits decorative. So DENY is absolute
and is evaluated before any allow rule, and paths are resolved before either.

A NOTE ON WHERE THIS SITS
---------------------------
Everywhere else in this codebase the Python layer is defence in depth over a
real boundary. Here it IS the boundary — the first load-bearing security check
in Comrade written in Python, in the same interpreter as the tool it gates.
That is a genuine step down in assurance and the reason the deny-list is
absolute rather than merely first. OS-level containment belongs underneath
this eventually; it does not replace it.
"""
import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

# There is deliberately no module-level root here.
#
# The first version of this file had `PROJECT_ROOT = <Comrade's own tree>`,
# which was wrong twice over: a team's agent has no business in Comrade's
# source, and a writable scope over `agent/**` would have let the agent edit
# this very file — the list of secrets it is not allowed to read.
#
# The root is now passed per call, derived from `team_id` by
# shared/workspace.py. One team, one tree, and Comrade's own source is not in
# any of them. That dissolves the self-modification problem rather than
# carving an exception for it.

# Absolute. No ArgPolicy can allow these and no allow rule overrides them.
# Matched against the resolved path AND its bare filename, so `.env` and
# `supabase/.env` are the same answer.
SECRET_GLOBS: tuple[str, ...] = (
    ".env", ".env.*", "*.env",
    "*.key", "*.pem", "*.p12", "*.pfx",
    "id_rsa", "id_ed25519", "*.ppk",
    "service-account*.json", "credentials.json", "*credentials*.json",
    ".npmrc", ".pypirc", ".netrc",
    "*.sqlite3-journal",
)

# A command containing any of these is refused outright rather than parsed.
# This is the difference between an allowlist that means something and one
# that does not: every one of `uv run pytest; cat .env`, `... && cat .env`,
# `... $(cat .env)` starts with an allowed prefix and then does something
# else. Parsing a shell correctly is hard enough that a half-parsed one is
# worse than no allowlist at all, so anything that could chain, redirect,
# substitute or background is simply not run.
_SHELL_METACHARACTERS = re.compile(r"[;&|<>`$\n\r()]")


class CapabilityError(Exception):
    """A tool was pointed at something its policy does not cover."""


@dataclass(frozen=True)
class ArgPolicy:
    """The scope of one tool's arguments.

    Empty means empty. A tool whose author declared no globs reaches no files,
    the same way registry.UNKNOWN refuses an undeclared tool — forgetting to
    declare a scope must fail closed, not open.
    """

    #: Globs, relative to PROJECT_ROOT, this tool may READ.
    allow: tuple[str, ...] = ()
    #: Globs it may WRITE. A subset in practice, and never implied by `allow`:
    #: a tool that reads broadly and writes narrowly is the common case, and
    #: inferring writes from reads would silently promote every reader.
    writable: tuple[str, ...] = ()
    #: Command prefixes a shell tool may run.
    commands: tuple[str, ...] = ()
    #: Which argument holds the path / the command, for the gate to find.
    path_arg: str | None = None
    command_arg: str | None = None
    #: "This tool takes no single path argument; it DERIVES paths and checks
    #: each one itself." True for a glob or a grep, whose arguments are a
    #: pattern and a search string — neither of which is a path, and both of
    #: which would be nonsense to resolve. `repo_grep("../old_name")` is a
    #: perfectly good search for a literal string and must not be refused for
    #: containing "..".
    #:
    #: This exists so the chokepoint can tell FORGOTTEN from DECLARED. A
    #: sandbox tool with neither a path_arg nor this flag is unscopable by
    #: omission and is refused; one carrying this flag has said out loud that
    #: it does its own checking, and a reviewer can go and look.
    derives_paths: bool = False
    #: Writes permitted per turn. The RATE dimension: a loop that has decided
    #: to rewrite the repository should be stopped by arithmetic, not noticed
    #: afterwards.
    max_writes_per_turn: int = 20


def _is_secret(resolved: Path) -> bool:
    name = resolved.name
    rel = resolved.as_posix()
    return any(
        fnmatch.fnmatch(name, g) or fnmatch.fnmatch(rel, f"*/{g}")
        for g in SECRET_GLOBS
    )


def check_path(raw: str, policy: ArgPolicy, *, root: Path, writing: bool) -> str:
    """Resolve `raw` and confirm the policy covers it. Returns the real path.

    Order matters and is the whole design: RESOLVE, then containment, then
    DENY, then ALLOW. Resolving first is what makes `agent/../.env` and a
    symlink out of the tree answerable at all — a textual prefix check passes
    both. Denying before allowing is what makes the deny-list absolute rather
    than merely consulted.
    """
    root = Path(root).resolve()
    candidate = Path(raw)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()

    try:
        relative = resolved.relative_to(root)
    except ValueError:
        raise CapabilityError(
            f"{raw!r} resolves to {resolved}, which is outside this team's"
            " workspace. Tools reach one team's checkout and nothing else —"
            " not another team's, and not Comrade's own source."
        ) from None

    if relative.parts and relative.parts[0] == ".git":
        # Neither readable nor writable, which is stricter than Codex — it
        # keeps .git read-only rather than closed. Two reasons for the extra
        # step, and a correction:
        #
        # WRITING: history is how a change gets reviewed and reverted. An agent
        # that can rewrite it can erase what it did.
        #
        # READING: .git is where a git credential would be cached if one ever
        # were. pipeline/repo_sync.py deliberately keeps the token out of the
        # remote URL and has a test that greps the whole directory for it — but
        # that is one version of one tool behaving as documented today, and
        # `.git` holds nothing an agent needs. Branch and history reach the
        # model through repo_activity, already built and already datamarked.
        #
        # This was written as write-only first, while two other files claimed
        # `.git` was "denied entirely". A read test caught the gap. Closing it
        # is cheaper than keeping three files agreeing about a carve-out.
        raise CapabilityError(
            f"{raw!r} is inside .git, which is not readable or writable."
            " Repository history reaches you through repo_activity; change"
            " files and let the commit be made for you."
        )

    if _is_secret(resolved):
        raise CapabilityError(
            f"{raw!r} is a secret file and is never readable, whatever a"
            " tool's scope says. It holds credentials that would bypass every"
            " other control in this system."
        )

    globs = policy.writable if writing else policy.allow
    if not globs:
        raise CapabilityError(
            f"this tool declares no {'writable' if writing else 'readable'}"
            f" scope, so it may not touch {raw!r}."
            + ("" if writing else " Declare one in agent/registry.py.")
        )

    rel = relative.as_posix()
    if not any(fnmatch.fnmatch(rel, g) for g in globs):
        if writing and any(fnmatch.fnmatch(rel, g) for g in policy.allow):
            raise CapabilityError(
                f"{raw!r} is read-only for this tool: it is inside the"
                f" readable scope but not the writable one."
            )
        raise CapabilityError(
            f"{raw!r} is not in this tool's scope. Permitted:"
            f" {', '.join(globs)}."
        )
    return str(resolved)


def check_command(raw: str, allowed: tuple[str, ...]) -> str:
    """Confirm `raw` is one of `allowed`, and that it is only that.

    Two checks, and the second is the one that matters. A prefix allowlist on
    its own is defeated by every shell operator there is, so a command
    carrying any of them is refused rather than parsed — see
    _SHELL_METACHARACTERS.
    """
    command = raw.strip()
    if not command:
        raise CapabilityError("empty command")

    found = _SHELL_METACHARACTERS.search(command)
    if found:
        raise CapabilityError(
            f"the command contains {found.group()!r}, and shell operators are"
            " refused: they let an allowed prefix be followed by anything at"
            " all. Run one command, with no chaining, redirection or"
            " substitution."
        )

    if not allowed:
        raise CapabilityError("this tool declares no allowed commands.")

    for prefix in allowed:
        # Word boundary, so `git` admits `git status` and not `gitpush`, and
        # an entry for `git p` does not admit `git push`.
        if command == prefix or command.startswith(prefix + " "):
            return command

    raise CapabilityError(
        f"{command!r} is not an allowed command. Permitted:"
        f" {', '.join(allowed)}."
    )
