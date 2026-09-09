"""Whether the release gate can tell a served site from an unserved one.

🔴 THE DEFECT (fix.md F35). The check proved neither half of what it claimed.

`scripts/deploy_host.sh` asked the caddy container for
`https://localhost/api/health` with `--no-check-certificate`:

  * Caddy's site is `{$COMRADE_HOST}` — a NAMED site, matched on the Host header
    and SNI. A request for `localhost` matches no site, Caddy answers 404, and a
    perfectly healthy deployment was reported as "the API is ready but the
    public proxy is not serving it".
  * `--no-check-certificate` made every TLS failure invisible — an expired
    certificate, a wrong name, a broken ACME renewal — which is the one thing a
    proxy check exists to catch.

And it only ever asked for `/api/health`, so a frontend that built nothing, or
was not being served, sailed through the release gate.

TESTED AGAINST REAL TLS, because the acceptance asks for actual HTTP/TLS
behaviour rather than the shape of a shell command — and because the previous
version of this check would have passed any argv-level test that existed. Each
case below runs the real script against a real HTTPS server with a real
certificate, over a socket.
"""
import datetime
import http.server
import shutil
import socket
import ssl
import subprocess
import threading
from pathlib import Path

import pytest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "proxy_check.sh"
SH = shutil.which("sh") or "C:/Program Files/Git/bin/sh.exe"

#: The configured public name. Deliberately not localhost: that is the bug.
HOST = "comrade.example.test"

needs_curl = pytest.mark.skipif(
    shutil.which("curl") is None, reason="curl is how the check speaks HTTPS"
)


def _certificate(directory: Path, common_name: str) -> tuple[Path, Path]:
    """A self-signed certificate for `common_name`, and its key.

    Self-signed and used as its own root: the point is to control trust
    exactly, so the test can hand the script a CA that does or does not match
    what the server presents.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(common_name)]),
                       critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                       critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / f"{common_name}.pem"
    key_path = directory / f"{common_name}.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    return cert_path, key_path


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _Site:
    """A proxy, as far as the check is concerned: two upstreams behind TLS."""

    def __init__(self, *, api: int = 200, app: int = 200, served_name=HOST,
                 directory: Path):
        self.api = api
        self.app = app
        self.port = _free_port()
        self.cert, key = _certificate(directory, served_name)
        self.requests: list[tuple[str, str]] = []
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                site.requests.append((self.headers.get("Host", ""), self.path))
                status = site.api if self.path.startswith("/api/") else site.app
                body = b"ok" if status == 200 else b"not found"
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, key)
        self._server = http.server.HTTPServer(("127.0.0.1", self.port), Handler)
        self._server.socket = context.wrap_socket(
            self._server.socket, server_side=True)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        self._server.shutdown()
        # And CLOSE it. shutdown() only ends serve_forever; the socket stays
        # bound and listening, so a connection would still be accepted and the
        # "nothing listening" case would prove nothing.
        self._server.server_close()


def _check(site: _Site, *, host: str = HOST, ca: Path | None = ...,
           attempts: str = "1") -> subprocess.CompletedProcess:
    env = {
        "COMRADE_PROXY_PORT": str(site.port),
        "PATH": __import__("os").environ.get("PATH", ""),
        "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
    }
    trust = site.cert if ca is ... else ca
    if trust is not None:
        env["COMRADE_TLS_CA"] = str(trust)
    return subprocess.run(
        [SH, str(SCRIPT), host, "127.0.0.1", attempts],
        capture_output=True, text=True, timeout=180, env=env,
    )


@pytest.fixture
def site(tmp_path):
    made: list[_Site] = []

    def build(**kwargs):
        instance = _Site(directory=tmp_path, **kwargs)
        made.append(instance)
        return instance

    yield build
    for instance in made:
        instance.stop()


# ---------------------------------------------------------------------------

@needs_curl
def test_a_healthy_site_on_a_real_hostname_passes(site):
    """🔴 The case the old check FAILED. Nothing is wrong with this deployment;
    the only reason it was reported broken is that the probe asked for a
    hostname the proxy has no site for."""
    instance = site()

    done = _check(instance)

    assert done.returncode == 0, done.stderr
    assert "public DNS and routing not verified" in done.stdout


@needs_curl
def test_the_configured_hostname_is_what_reaches_the_proxy(site):
    """The Host header is how a named site is selected, so it is the thing
    that has to be right."""
    instance = site()

    _check(instance)

    assert instance.requests, "the proxy was never reached"
    for host_header, _path in instance.requests:
        assert host_header.split(":")[0] == HOST, (
            f"the proxy was asked for {host_header!r}, not the configured name"
        )


@needs_curl
def test_both_upstreams_are_checked(site):
    """A frontend that serves nothing while /api/health is healthy used to
    reach 'deployed'."""
    instance = site()

    _check(instance)

    paths = {path for _host, path in instance.requests}
    assert "/api/health" in paths
    assert "/" in paths


@needs_curl
def test_a_stopped_frontend_fails_the_gate(site):
    instance = site(app=404)

    done = _check(instance)

    assert done.returncode == 1
    assert "frontend" in done.stderr


@needs_curl
def test_broken_proxy_routing_fails_the_gate(site):
    """404 from the API upstream is what a Caddy with the wrong handle_path,
    or an api container that never came up, actually looks like."""
    instance = site(api=404)

    done = _check(instance)

    assert done.returncode == 1
    assert "error status" in done.stderr


@needs_curl
def test_an_untrusted_certificate_fails_the_gate(site):
    """🔴 The half `--no-check-certificate` threw away. Without the CA, this
    certificate is exactly what a broken ACME renewal leaves behind."""
    instance = site()

    done = _check(instance, ca=None)

    assert done.returncode == 1
    assert "certificate" in done.stderr


@needs_curl
def test_a_certificate_for_the_wrong_name_fails_the_gate(site):
    """A certificate that is valid and is not for this site. Verification that
    does not check the NAME is not verification."""
    instance = site(served_name="somewhere.else.test")

    done = _check(instance, ca=instance.cert)

    assert done.returncode == 1
    assert "certificate" in done.stderr


@needs_curl
def test_nothing_listening_fails_the_gate(site):
    instance = site()
    instance.stop()

    done = _check(instance)

    assert done.returncode == 1
    assert "listening" in done.stderr


def test_localhost_is_refused_as_the_configured_host(site, tmp_path):
    """The original defect, refused outright rather than checked. A named site
    has no answer for localhost, so a check through it is meaningless — and
    that meaninglessness is what read as a broken deployment."""
    instance = site()

    done = _check(instance, host="localhost")

    assert done.returncode == 2
    assert "named site" in done.stderr


def test_the_release_uses_the_shared_check_rather_than_its_own(site):
    """The deploy script must not keep a second, weaker copy of this."""
    script = (ROOT / "scripts" / "deploy_host.sh").read_text(encoding="utf-8")
    # Comments stripped: the file explains the defect it replaced, and the
    # explanation naturally contains the strings the CODE must not.
    code = "\n".join(line for line in script.splitlines()
                     if not line.lstrip().startswith("#"))

    assert "proxy_check.sh" in code
    assert "--no-check-certificate" not in code
    assert "https://localhost/api/health" not in code
