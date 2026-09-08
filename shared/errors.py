"""Turning an exception into something safe to store and ship.

🔴 THE DEFECT. Every failure path recorded `str(exc)` verbatim — into
`jobs.last_error`, into `agent_runs.last_error`, and into the log. A database
error is not a short sentence: Postgres attaches `DETAIL: Failing row contains
(...)`, which is THE ROW, every column of it in order. So a constraint
violation while compiling a document or writing a message wrote that content
into `jobs.last_error` — a column `comrade_control` can read across every team
— and into a log that is shipped somewhere else again.

The same shape as the defect T26 closed one layer down: the queue was
carefully denied a team's content and the queue's ERROR column was handing it
over anyway.

The rule here is that the DIAGNOSIS survives and the DATA does not. An error
report that has been reduced to "something went wrong" is not a redaction, it
is an outage with extra steps — so the exception class, the constraint name and
the failing statement's shape all stay, and the values do not.
"""
import re

import psycopg

#: What an operator can act on without seeing a row.
MAX_ERROR_CHARS = 600

#: Postgres appends these, and they are where the row goes. `DETAIL` on a
#: constraint violation is the whole failing row; `CONTEXT` is the statement
#: that produced it, parameters included.
_PG_SECTIONS = re.compile(
    r"\n(?:DETAIL|CONTEXT|HINT|QUERY|STATEMENT|PL/pgSQL function)\s*:.*",
    re.IGNORECASE | re.DOTALL,
)

#: Anything shaped like a credential, wherever it appears. Deliberately
#: pattern-based rather than a list of known variables: the leak that matters
#: is the one nobody enumerated, and a false positive costs a few characters of
#: an error message.
_SECRETS: tuple[tuple[re.Pattern[str], str], ...] = (
    # A DSN's password. Keeps the user and host, which are the diagnosis.
    (re.compile(r"(?<=:)//([^:/\s]+):[^@/\s]+@"), r"//\1:REDACTED@"),
    (re.compile(r"\bpassword\s*=\s*\S+", re.IGNORECASE), "password=REDACTED"),
    # GitHub tokens, all current prefixes.
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "REDACTED_TOKEN"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "REDACTED_TOKEN"),
    # A PEM block, however it is wrapped.
    (re.compile(r"-----BEGIN[^-]*-----.*?-----END[^-]*-----", re.DOTALL),
     "REDACTED_KEY"),
    (re.compile(r"-----BEGIN[^-]*-----.*", re.DOTALL), "REDACTED_KEY"),
    # A JWT: three base64url segments. Supabase user tokens and GitHub App
    # tokens both look like this.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"),
     "REDACTED_JWT"),
    # Bearer headers and the generic `key=`/`token=`/`secret=` shapes.
    (re.compile(r"\bBearer\s+\S+", re.IGNORECASE), "Bearer REDACTED"),
    (re.compile(r"\b(api[_-]?key|secret|token)\s*[=:]\s*\S+", re.IGNORECASE),
     r"\1=REDACTED"),
)


def redact(text: str) -> str:
    """Strip credentials and Postgres' row dumps out of a message."""
    text = _PG_SECTIONS.sub("", text)
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text.strip()


#: Packages whose exceptions are raised with a sentence somebody wrote.
_OURS = ("agent.", "pipeline.", "server.", "shared.")

#: The parts of a database error that are SCHEMA rather than DATA. Every one of
#: these is an identifier the developer chose; none can carry a row value.
_DIAG_FIELDS = (
    ("constraint", "constraint_name"),
    ("table", "table_name"),
    ("column", "column_name"),
    ("datatype", "datatype_name"),
)


def database_error(exc: psycopg.Error) -> str:
    """A database failure described structurally, with no message text at all.

    🔴 Stripping DETAIL and CONTEXT was not enough. PostgreSQL puts values in
    the PRIMARY message too — `invalid input syntax for type uuid: "…"`,
    `invalid input value for enum …` — so a malformed identifier from a model,
    a document, or a member landed intact in `jobs.last_error`, which the
    cross-team control role can read, and in the log. The regexes above cannot
    help: a filename or a sentence is not shaped like a secret.

    So for database errors nothing textual survives. The exception class, the
    SQLSTATE, and the identifiers Postgres reports separately are the whole
    output — all of them schema, none of them data, and between them they say
    what an operator needs: which kind of failure, and which constraint or
    column it was about.
    """
    parts = [type(exc).__name__]
    if exc.sqlstate:
        parts.append(f"[{exc.sqlstate}]")
    diag = exc.diag
    for label, attribute in _DIAG_FIELDS:
        value = getattr(diag, attribute, None)
        if value:
            parts.append(f"{label}={value}")
    return " ".join(parts)


def safe_error(exc: BaseException) -> str:
    """`exc` as a line that is safe to store in a table and ship to a log.

    A LIBRARY exception keeps its class name. It is never the sensitive part
    and it is most of what an operator needs — "OperationalError" and
    "ValueError" are different problems with different responses, and a message
    redacted down to nothing still says which one happened.

    OUR OWN exceptions do not. `PermanentJobError`, `DocumentTooLarge`,
    `BudgetExceeded` and the rest are raised with a sentence somebody wrote for
    a person to read, and several of these strings are shown to MEMBERS —
    `jobs.last_error` is what the connect screen renders when a clone fails. A
    Python class name in front of "no GitHub credential reaches acme/app" helps
    nobody and makes the product look broken in a different way than it is.

    A DATABASE exception keeps no message text at all: `database_error` builds
    the whole line from the class, the SQLSTATE and Postgres' own identifier
    fields, because the primary message carries values that no pattern can be
    trusted to recognise.
    """
    if isinstance(exc, psycopg.Error):
        # Structural, not textual. See `database_error`.
        return database_error(exc)
    message = redact(str(exc))
    if not message:
        return type(exc).__name__
    if type(exc).__module__.startswith(_OURS):
        line = message
    else:
        line = f"{type(exc).__name__}: {message}"
    if len(line) > MAX_ERROR_CHARS:
        line = line[: MAX_ERROR_CHARS - 1] + "…"
    return line
