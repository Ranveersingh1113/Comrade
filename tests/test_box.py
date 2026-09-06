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


def test_command_uses_fixed_argv_and_never_retries_gateway_failure():
    from agent.box import BoxClient, BoxCommandAmbiguous

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(502, json={"code": "box_direct_failed"})

    client = BoxClient("box-test", transport=httpx.MockTransport(handler))
    with __import__("pytest").raises(BoxCommandAmbiguous):
        client.run("bx_23456789", ["pytest", "-q"], timeout=120)
    assert len(calls) == 1
    assert json.loads(calls[0].content) == {
        "command": "pytest -q", "cwd": ".",
        "timeoutSeconds": 120, "detached": False,
    }


def test_command_can_run_in_a_project_and_detach():
    from agent.box import BoxClient

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"processId": "proc-1"})

    client = BoxClient("box-test", transport=httpx.MockTransport(handler))
    assert client.run(
        "bx_23456789", ["npm", "run", "dev"], timeout=120,
        cwd="comrade-project", detached=True,
    ) == {"processId": "proc-1"}
    assert json.loads(seen[0].content) == {
        "command": "npm run dev", "cwd": "comrade-project",
        "timeoutSeconds": 120, "detached": True,
    }


def test_get_and_delete_use_only_the_box_id():
    from agent.box import BoxClient

    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"box": {"state": "ready"}})

    client = BoxClient("box-test", transport=httpx.MockTransport(handler))
    assert client.get("bx_23456789")["state"] == "ready"
    client.delete("bx_23456789")
    assert [(r.method, r.url.path) for r in requests] == [
        ("GET", "/api/box/v1/boxes/bx_23456789"),
        ("DELETE", "/api/box/v1/boxes/bx_23456789"),
    ]
    assert requests[1].headers["x-ascii-confirm-delete"] == "bx_23456789"


def test_write_file_base64_encodes_bytes_and_keeps_path_in_box():
    from agent.box import BoxClient

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "type": "file.written"})

    client = BoxClient("box-test", transport=httpx.MockTransport(handler))
    client.write_file("bx_23456789", "/home/user/upload.tar.gz", b"\x00source")
    assert json.loads(seen[0].content) == {
        "path": "/home/user/upload.tar.gz", "content": "AHNvdXJjZQ==", "encoding": "base64"
    }
