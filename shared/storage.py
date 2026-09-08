"""Reading a team's uploaded files back out of private Storage.

🔴 WHY THIS EXISTS. The document queue used to carry the whole file:
`jobs.payload` held the complete document, base64 for pdf and docx. And
`comrade_control` — the cross-team maintenance role that claims jobs, sweeps
queues and reaps leases — holds `select (… payload …)` on that table. It could
read the contents of every document every team had ever uploaded, because the
queue was carrying them past it.

The bytes were already in private Storage. The job only ever needed to say
WHICH file, and the worker fetches it under its own permission when it is
ready to parse.
"""
import httpx

from shared.config import settings

#: Where uploaded documents live.
DOCUMENT_BUCKET = "documents"

#: The most one document may weigh.
#:
#: 🔴 Nothing bounded this. A parser allocating a gigabyte is the worker dying,
#: and every other team's jobs waiting behind the restart — one team's upload
#: becoming everybody's outage.
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024


class DocumentTooLarge(RuntimeError):
    """The file is bigger than this system will parse. Not retryable."""


class ObjectNotOwned(RuntimeError):
    """The object is not this document's to read. Not retryable.

    🔴 (fix.md F20, reopened.) `download_document` took a path and nothing
    else, and fetched it with the SERVICE SECRET — which is not subject to RLS.
    `documents.storage_path` is written by members, so knowing another team's
    object path, or a restricted same-team attachment's, was enough to have the
    pipeline read it and compile the text into a wiki the member can see.

    The UNIQUE index added with the first repair closes aliasing to an object
    that already HAS a document row. It cannot see the window where one does
    not: an upload whose metadata insert never landed, or a row that was
    hard-deleted.
    """


def canonical_path(path: str) -> str | None:
    """The path if we will send it to Storage at all, else None.

    REJECTED, not repaired. Normalising `a/../b` into something acceptable
    means deciding what it points at, and the Storage API may decide
    differently — two answers to "where does this point" is the whole class of
    bug this guard exists for.
    """
    if not path or path != path.strip():
        return None
    if path.startswith("/") or "\\" in path:
        return None
    segments = path.split("/")
    if len(segments) < 2:
        return None
    # An empty segment is `//`; `.` and `..` traverse. Any of the three means
    # the string does not name what it appears to name.
    if any(segment in ("", ".", "..") for segment in segments):
        return None
    return path


def assert_object_is_authentic(
    storage_path: str, *, team_id: str, document_id: str,
) -> str:
    """Refuse to read an object this document did not upload.

    Returns the path when it is safe, so a caller cannot use the unchecked one
    by accident.

    Two checks, and the second is the one that matters: the path must sit under
    the owning team's prefix, AND the object's Storage `owner_id` must be the
    member the document row claims uploaded it. A restricted same-team
    attachment passes the prefix and fails the owner, which is exactly why the
    prefix alone was never enough.
    """
    safe = canonical_path(storage_path)
    if safe is None or safe.split("/")[0] != str(team_id):
        raise ObjectNotOwned(
            "this document does not name a file belonging to this team"
        )
    from shared.db import Role, team_session

    with team_session(Role.PIPELINE, team_id) as conn:
        authentic = conn.execute(
            "select public.document_object_is_authentic(%s, %s)",
            (document_id, safe),
        ).fetchone()[0]
    if not authentic:
        raise ObjectNotOwned(
            "this document does not name a file belonging to this team"
        )
    return safe


#: How much of an upload to move at a time. Small enough that the refusal
#: happens long before the allocation matters.
UPLOAD_CHUNK = 1024 * 1024


def hash_upload(stream, *, max_bytes: int = MAX_DOCUMENT_BYTES) -> str:
    """The sha256 of an upload, without ever holding it.

    🔴 (fix.md F23) The API did `hashlib.sha256(file.file.read())` — the whole
    file into memory purely to compute an identity it then threw away. The
    25 MiB cap covered the worker's DOWNLOAD and neither of the API's reads, so
    the bound existed on the path where the bytes were already known to be
    fine and not on the one a caller controls.
    """
    import hashlib

    digest = hashlib.sha256()
    total = 0
    while chunk := stream.read(UPLOAD_CHUNK):
        total += len(chunk)
        if total > max_bytes:
            raise DocumentTooLarge(
                f"file is over the {max_bytes:,} byte limit"
            )
        digest.update(chunk)
    return digest.hexdigest()


def read_upload(stream, *, max_bytes: int = MAX_DOCUMENT_BYTES) -> bytes:
    """An upload's bytes, or a refusal — counted as they arrive.

    Measuring after `read()` is measuring after the allocation already
    happened, which is the thing the cap exists to prevent.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := stream.read(UPLOAD_CHUNK):
        total += len(chunk)
        if total > max_bytes:
            raise DocumentTooLarge(
                f"file is over the {max_bytes:,} byte limit"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def download_document(
    storage_path: str, *, team_id: str, document_id: str,
    max_bytes: int = MAX_DOCUMENT_BYTES,
) -> bytes:
    """Read a stored document back with the service key, under a size cap.

    `team_id` and `document_id` are REQUIRED and are not decoration: the key
    used here bypasses RLS, so this function is the last place that can ask
    whether the caller is entitled to these bytes, and it used to ask nothing
    (fix.md F20).

    Streamed and counted rather than read whole and measured afterwards:
    measuring after the fact means the allocation already happened, which is
    the thing the cap exists to prevent.
    """
    storage_path = assert_object_is_authentic(
        storage_path, team_id=team_id, document_id=document_id,
    )
    url = (
        f"{settings.supabase_url.rstrip('/')}/storage/v1/object/"
        f"{DOCUMENT_BUCKET}/{storage_path.lstrip('/')}"
    )
    headers = {
        "Authorization": f"Bearer {settings.supabase_secret_key}",
        "apikey": settings.supabase_secret_key,
    }
    with httpx.stream("GET", url, headers=headers, timeout=60.0) as response:
        response.raise_for_status()
        declared = response.headers.get("content-length")
        if declared and int(declared) > max_bytes:
            raise DocumentTooLarge(
                f"file is {int(declared):,} bytes, limit is {max_bytes:,}"
            )
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > max_bytes:
                # A declared length can be absent or wrong; this is the check
                # that actually holds.
                raise DocumentTooLarge(
                    f"file exceeds {max_bytes:,} bytes"
                )
            chunks.append(chunk)
    return b"".join(chunks)
