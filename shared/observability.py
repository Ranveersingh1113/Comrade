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


class RedactingFormatter(logging.Formatter):
    """Wrap another formatter and redact whatever it produces.

    🔴 The first version of this mutated the RECORD — it rendered the message,
    redacted the text, and set `record.args = ()`. That works for a plain
    Formatter and breaks any formatter that reads the arguments itself.
    uvicorn's `AccessFormatter` does exactly that: it builds the request line
    from `record.args`, so an emptied tuple made every access log call raise,
    and each raise printed a traceback to stdout. Under a captured pipe that
    filled the buffer and WEDGED THE SERVER — a privacy fix that stopped the
    product answering requests.
    #
    Redacting the finished string instead is both safer and more general: it
    does not care what built the text, it covers the traceback that
    `Formatter.format` already appended, and it cannot disagree with the
    formatter it wraps about what the arguments mean.
    """

    def __init__(self, inner: logging.Formatter) -> None:
        super().__init__()
        self.inner = inner

    def format(self, record: logging.LogRecord) -> str:
        return redact(self.inner.format(record))


class RequestLineFilter(logging.Filter):
    """Strip the query string out of an access-log line.

    🔴 A query string is where VALUES live — a document name, a search term, a
    token somebody pasted into a URL — and the redactor cannot help with most
    of them, because they are not shaped like secrets. The route is the part an
    operator needs; the arguments are the part that should never have been
    written down.

    uvicorn's access records carry
    `(client_addr, method, full_path, http_version, status_code)` and its
    formatter builds the request line from them, so the path is rewritten in
    place rather than by parsing the finished string.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            path = args[2]
            if isinstance(path, str) and "?" in path:
                # Rewritten in place and kept a 5-tuple: uvicorn's
                # AccessFormatter reads these positionally, and handing it a
                # different shape is how the redaction above once wedged the
                # server.
                record.args = (*args[:2], path.split("?", 1)[0] + "?…", *args[3:])
        return True


def configure(handler: logging.Handler) -> logging.Handler:
    """Give a handler correlation fields and redaction. Both, always — a
    formatted line with no redaction is the leak this module exists to close."""
    handler.setFormatter(RedactingFormatter(ContextFormatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )))
    return handler


def redact_handler(handler: logging.Handler) -> None:
    """Make an EXISTING handler redact, keeping how it already formats.

    For the handlers somebody else configured — uvicorn's, chiefly — where the
    formatting is theirs and only the leak is ours.
    """
    if isinstance(handler.formatter, RedactingFormatter):
        return
    handler.setFormatter(RedactingFormatter(
        handler.formatter or logging.Formatter()
    ))


#: Loggers that carry request lines rather than application messages.
ACCESS_LOGGERS = ("uvicorn.access", "gunicorn.access", "hypercorn.access")


def setup(service: str, level: int = logging.INFO) -> None:
    """Install the redacting handler everywhere this process logs.

    Called by the API and both workers instead of `logging.basicConfig`, which
    installs a handler with neither the formatter nor the filter.

    🔴 This used to replace ROOT's handlers and stop, and root is not where
    uvicorn logs. The `uvicorn` CLI applies its packaged LOGGING_CONFIG before
    it imports the app, and that gives `uvicorn.access` its own handler with
    `propagate: false` — so every request line and query string went to stdout
    without ever passing the redactor. A filter attached to one handler is not
    a property of the process.

    So: root gets ours, and every logger that already has handlers of its own
    gets the same filter attached to them. Ordering is why this works — by the
    time an app module imports, uvicorn has already configured itself.
    """
    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(configure(logging.StreamHandler()))

    for name, logger in list(logging.root.manager.loggerDict.items()):
        if not isinstance(logger, logging.Logger):
            continue          # a PlaceHolder for an unconfigured parent
        for handler in logger.handlers:
            redact_handler(handler)
            if name in ACCESS_LOGGERS and not any(
                isinstance(f, RequestLineFilter) for f in handler.filters
            ):
                handler.addFilter(RequestLineFilter())

    bind(service=service)
