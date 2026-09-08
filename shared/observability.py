"""Correlation fields on every log line, and redaction that is not optional.

🔴 THE DEFECTS.

1. Nothing correlated. Every log line was prose, and the ids in it were
   whichever ones that call site happened to interpolate. An operator handed
   "Comrade did nothing when I asked at 14:32" had no field to filter on — not
   the run, not the thread, not the team — and the answer was spread across
   the API, the agent worker and the pipeline worker.

2. Redaction was a convention. `shared/errors.py` cleans an error a caller
   remembers to pass through it, and a convention is exactly what the one
   careless line ignores. A log is the copy that leaves the building, so it
   needs a guarantee: the filter here runs over the FORMATTED record, message,
   arguments and traceback together, so a line nobody thought about is covered
   by the same rule as one that was.

Deliberately not a logging framework. `contextvars` carries the ids, one
formatter appends them, one filter cleans the result — the whole thing is
readable in a sitting, and it works with `logging.getLogger(__name__)` exactly
as every module already uses it.
"""
import contextvars
import logging
from contextlib import contextmanager
from typing import Any, Iterator

from shared.errors import redact

#: The fields worth filtering a log by, in the order an operator reads them:
#: which deployment, whose data, which piece of work.
FIELDS = ("service", "team_id", "thread_id", "run_id", "job_id", "job_type",
          "worker_id", "request_id")

_context: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "comrade_log_context", default={}
)


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Attach correlation fields to every log line inside this block.

    Scoped rather than global: a worker slot handles one team's job and then
    another's, and context that leaked between them would attribute one team's
    failure to another — worse than having none at all.
    """
    token = _context.set({**_context.get(), **{
        k: str(v) for k, v in fields.items() if v is not None
    }})
    try:
        yield
    finally:
        _context.reset(token)


def bind(**fields: Any) -> None:
    """Add fields to the context already open.

    A run id does not exist until the run row does, which is after the turn's
    context has been opened — so the block cannot always know everything at the
    moment it starts.
    """
    _context.set({**_context.get(), **{
        k: str(v) for k, v in fields.items() if v is not None
    }})


def context() -> dict[str, str]:
    """What the current block is carrying. For tests and for handlers that
    want to put the same ids somewhere other than a log."""
    return dict(_context.get())


class ContextFormatter(logging.Formatter):
    """`<time> <level> <logger>: <message> key=value ...`

    Trailing rather than leading, because the message is what a human reads
    first and the fields are what a query filters on.
    """

    default_time_format = "%Y-%m-%dT%H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        fields = _context.get()
        pairs = " ".join(
            f"{name}={fields[name]}" for name in FIELDS if fields.get(name)
        )
        # Anything the caller passed as `extra=` wins over the ambient
        # context: it is more specific by definition.
        explicit = " ".join(
            f"{name}={getattr(record, name)}"
            for name in FIELDS
            if hasattr(record, name) and not fields.get(name)
        )
        tail = " ".join(p for p in (pairs, explicit) if p)
        return f"{base} {tail}" if tail else base


class RedactingFilter(logging.Filter):
    """Run every record through `shared.errors.redact` before it is emitted.

    On the FORMATTED record — message, arguments and traceback together —
    because a credential arrives through any of the three. `logger.exception`
    is the common one: the traceback carries the exception's own text, and that
    is exactly where a DSN with a password ends up.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not kill logging
            return True
        cleaned = redact(rendered)
        if record.exc_info or record.exc_text:
            cleaned = f"{cleaned}\n{redact(_traceback(record))}"
            record.exc_info = None
            record.exc_text = None
        if cleaned != rendered or record.args:
            record.msg = cleaned
            record.args = ()
        return True


def _traceback(record: logging.LogRecord) -> str:
    if record.exc_text:
        return record.exc_text
    formatter = logging.Formatter()
    return formatter.formatException(record.exc_info)


def configure(handler: logging.Handler) -> logging.Handler:
    """Give a handler the formatter and the filter. Both, always — a formatted
    line with no redaction is the leak this module exists to close."""
    handler.setFormatter(ContextFormatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    handler.addFilter(RedactingFilter())
    return handler


def setup(service: str, level: int = logging.INFO) -> None:
    """Install the root handler for a service process.

    Called by the API and both workers instead of `logging.basicConfig`, which
    installs a handler with neither of the above.
    """
    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(configure(logging.StreamHandler()))
    bind(service=service)
