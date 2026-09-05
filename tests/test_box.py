"""ASCII Box boundary: no provider credentials or unsafe source cross it."""
import io
import json
import tarfile
from uuid import UUID

import httpx


def test_create_uses_noenv_and_stable_thread_idempotency_key():
    from agent.box import BoxClient

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": "box-1", "status": "ready"})

    client = BoxClient("box-test", transport=httpx.MockTransport(handler))
    thread_id = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
    team_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    assert client.create(thread_id, team_id)["id"] == "box-1"

    body = json.loads(seen[0].content)
    assert body["noEnv"] is True
    assert body["env"] == {"COMRADE_TEAM_ID": str(team_id), "COMRADE_THREAD_ID": str(thread_id)}
    assert seen[0].headers["idempotency-key"] == f"comrade-thread-{thread_id}"
    assert seen[0].headers["authorization"] == "Bearer box-test"


def test_archive_excludes_secret_git_and_escaping_symlink(tmp_path):
    from agent.box import build_source_archive

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n")
    (tmp_path / ".env").write_text("secret\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("token\n")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("do not upload\n")
    (tmp_path / "escape").symlink_to(outside)

    archive, fingerprint = build_source_archive(tmp_path, max_bytes=1_000_000)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        assert tar.getnames() == ["src/app.py"]
    assert fingerprint
