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
    storage_path: str, *, max_bytes: int = MAX_DOCUMENT_BYTES,
) -> bytes:
    """Read a stored document back with the service key, under a size cap.

    Streamed and counted rather than read whole and measured afterwards:
    measuring after the fact means the allocation already happened, which is
    the thing the cap exists to prevent.
    """
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
