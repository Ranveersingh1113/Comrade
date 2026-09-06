"""Small, server-only client for the ASCII Box execution boundary."""
import hashlib
import io
import base64
import shlex
import tarfile
from pathlib import Path
from uuid import UUID

import httpx


API_URL = "https://ascii.dev/api/box/v1"


class BoxError(Exception):
    """A Box request failed before a repository command ran."""


class BoxStarting(BoxError):
    """The Box is provisioning; its lifecycle may be polled and retried."""


class BoxCommandAmbiguous(BoxError):
    """A gateway failure may have started the command; never retry it."""


class BoxClient:
    def __init__(self, api_key: str, *, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            base_url=API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
            timeout=30,
        )

    def create(self, thread_id: UUID, team_id: UUID) -> dict:
        response = self._client.post(
            "/boxes",
            headers={"Idempotency-Key": f"comrade-thread-{thread_id}"},
            json={
                "noEnv": True,
                "ttlSeconds": 900,
                "env": {
                    "COMRADE_TEAM_ID": str(team_id),
                    "COMRADE_THREAD_ID": str(thread_id),
                },
            },
        )
        if response.status_code >= 400:
            self._raise(response)
        body = response.json()
        return body.get("box", body)

    def run(self, box_id: str, argv: list[str], *, timeout: int) -> dict:
        """Execute one already-authorized argv; ambiguous submission is fatal."""
        try:
            response = self._client.post(
                f"/boxes/{box_id}/commands",
                json={
                    "command": shlex.join(argv),
                    "cwd": ".",
                    "timeoutSeconds": timeout,
                    "detached": False,
                },
            )
        except httpx.TransportError as exc:
            raise BoxCommandAmbiguous("Box command submission state is unknown") from exc
        if response.status_code >= 500:
            raise BoxCommandAmbiguous("Box command submission state is unknown")
        if response.status_code >= 400:
            self._raise(response)
        return response.json()

    def get(self, box_id: str) -> dict:
        response = self._client.get(f"/boxes/{box_id}")
        if response.status_code >= 400:
            self._raise(response)
        body = response.json()
        return body.get("box", body)

    def write_file(self, box_id: str, path: str, content: bytes) -> None:
        if not (path.startswith("/home/user/") or path.startswith("/tmp/")):
            raise BoxError("Box file path must be under /home/user or /tmp")
        response = self._client.put(
            f"/boxes/{box_id}/files",
            json={
                "path": path,
                "content": base64.b64encode(content).decode(),
                "encoding": "base64",
            },
        )
        if response.status_code >= 400:
            self._raise(response)

    def delete(self, box_id: str) -> None:
        response = self._client.delete(
            f"/boxes/{box_id}", headers={"X-Ascii-Confirm-Delete": box_id}
        )
        if response.status_code >= 400:
            self._raise(response)

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = body.get("message", f"Box request failed ({response.status_code})")
        if body.get("code") == "box_starting":
            raise BoxStarting(message)
        raise BoxError(message)


def build_source_archive(root: Path, *, max_bytes: int) -> tuple[bytes, str]:
    """Archive only regular, non-secret files physically contained by root."""
    root = root.resolve()
    digest = hashlib.sha256()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if (relative.parts[0] == ".git" or path.name == ".env"
                    or path.name.startswith(".env.")):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            data = path.read_bytes()
            digest.update(relative.as_posix().encode() + b"\0" + data)
            info = tarfile.TarInfo(relative.as_posix())
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
            if stream.tell() > max_bytes:
                raise BoxError(f"repository archive exceeds {max_bytes} byte limit")
    data = stream.getvalue()
    if len(data) > max_bytes:
        raise BoxError(f"repository archive exceeds {max_bytes} byte limit")
    return data, digest.hexdigest()
