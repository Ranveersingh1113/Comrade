"""The proxy in front of the product, and whether it can start at all.

🔴 THE DEFECT (fix.md F01). The preview site lived in `docker/Caddyfile`
unconditionally, and previews are OFF by default. So an ordinary deployment —
one that had simply not set `COMRADE_PREVIEW_DOMAIN` — rendered `*.` as a site
address and `dns` with no arguments, and the real `caddy:2-alpine` image
refused the entire file:

    Error: adapting config using caddyfile: parsing caddyfile tokens for
    'tls': wrong argument count or unexpected line ending after 'dns'

Caddy exits, the public site is down, and NOTHING NOTICED: the release
readiness check in `scripts/deploy_host.sh` ran inside the api container
against `localhost:8000`, so it passed while the product was unreachable. A
deploy printed "deployed <sha>" over an outage.

These tests run the real image, because the whole point is that the previous
configuration looked fine and the image would not take it.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CADDYFILE = ROOT / "docker" / "Caddyfile"
CONF_D = ROOT / "docker" / "caddy.conf.d"
FRAGMENT = ROOT / "docker" / "caddy-previews.caddy"
IMAGE = "caddy:2-alpine"

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="needs Docker to run the real image"
)


def _validate(env: dict[str, str], conf_d: Path) -> subprocess.CompletedProcess:
    """Ask the image that will actually run this whether it will take it."""
    def mount(path: Path, target: str) -> list[str]:
        # Git Bash on Windows rewrites container paths unless this is set; the
        # host side has to be a Windows path for the daemon to resolve it.
        return ["-v", f"{path.resolve()}:{target}:ro"]

    argv = ["docker", "run", "--rm"]
    for key, value in env.items():
        argv += ["-e", f"{key}={value}"]
    argv += mount(CADDYFILE, "/etc/caddy/Caddyfile")
    argv += mount(conf_d, "/etc/caddy/conf.d")
    argv += [IMAGE, "caddy", "validate", "--config", "/etc/caddy/Caddyfile",
             "--adapter", "caddyfile"]
    return subprocess.run(  # noqa: S603 - fixed argv, never a shell string
        argv, capture_output=True, text=True, timeout=300,
        env={**__import__("os").environ, "MSYS_NO_PATHCONV": "1"},
    )


# ---------------------------------------------------------------------------

def test_the_ordinary_deployment_validates(tmp_path):
    """🔴 It did not. Previews off is the DEFAULT and the documented supported
    configuration, and it was the one the image refused."""
    empty = tmp_path / "conf.d"
    empty.mkdir()

    done = _validate(
        {"COMRADE_HOST": "example.test", "COMRADE_PREVIEW_DOMAIN": ""}, empty,
    )

    assert done.returncode == 0, done.stderr[-2000:]


def test_the_shipped_conf_d_is_empty_of_sites(tmp_path):
    """The default has to stay the default. A fragment committed into this
    directory would turn previews on for every deployment that pulls."""
    sites = sorted(p.name for p in CONF_D.glob("*.caddy"))

    assert sites == [], (
        f"docker/caddy.conf.d ships site fragments: {sites}. They belong in"
        " docker/caddy-previews.caddy until an operator opts in."
    )


def test_enabling_previews_without_a_dns_module_fails_clearly(tmp_path):
    """A wildcard certificate is issued over DNS and the stock image has no
    provider module, so this configuration cannot work — what matters is that
    it says so, and says so BEFORE activation."""
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    shutil.copy(FRAGMENT, conf_d / "previews.caddy")

    done = _validate({
        "COMRADE_HOST": "example.test",
        "COMRADE_PREVIEW_DOMAIN": "p.example.test",
        "COMRADE_DNS_PROVIDER": "cloudflare",
        "COMRADE_DNS_TOKEN": "token",
    }, conf_d)

    assert done.returncode != 0
    assert "module not registered" in done.stderr, done.stderr[-2000:]


def test_an_incomplete_preview_configuration_fails(tmp_path):
    """Half-enabled is the dangerous state: the fragment is in place and a
    variable is missing. It must not render into something Caddy accepts."""
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    shutil.copy(FRAGMENT, conf_d / "previews.caddy")

    done = _validate({
        "COMRADE_HOST": "example.test",
        "COMRADE_PREVIEW_DOMAIN": "p.example.test",
        # provider and token unset
    }, conf_d)

    assert done.returncode != 0, (
        "a preview site with no DNS provider validated; it would then fail to"
        " obtain a certificate at runtime instead of before activation"
    )


# ---------------------------------------------------------------------------
# The release has to actually check
# ---------------------------------------------------------------------------

def test_the_release_validates_the_proxy_before_activating():
    """🔴 It did not, which is why a config the image refused could reach
    production. Validation belongs before `up`, while the healthy stack is
    still serving."""
    script = (ROOT / "scripts" / "deploy_host.sh").read_text(encoding="utf-8")

    validate_at = script.index("caddy \\\n    validate")
    activate_at = script.index("$COMPOSE up -d")

    assert validate_at < activate_at, (
        "the proxy configuration is validated after activation, which is after"
        " it can do any good"
    )


def test_the_release_checks_through_the_public_path():
    """🔴 The readiness check ran inside the api container against localhost,
    so a dead proxy was invisible to it. The last thing a release does has to
    be the thing a member does."""
    script = (ROOT / "scripts" / "deploy_host.sh").read_text(encoding="utf-8")

    assert "exec -T caddy" in script, (
        "nothing in the release speaks to the deployment through its proxy"
    )
    assert script.index("exec -T caddy") > script.index("$COMPOSE up -d")


def test_the_production_compose_mounts_the_fragment_directory():
    """The import is a glob into /etc/caddy/conf.d. Without the mount the
    directory does not exist in the container, and enabling previews would
    silently do nothing."""
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "/etc/caddy/conf.d" in compose
