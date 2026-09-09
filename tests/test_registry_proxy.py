"""The egress policy for the dependency-install phase.

🔴 WHY THIS IS A TEST AND NOT A COMMENT (fix.md F06, hackathon preflight).
`agent/sandbox.py:run_setup` executes a repository's own build hooks — pip's
setup.py, npm's postinstall — as root, with a network. It refuses to run unless
COMRADE_SETUP_PROXY_URL and COMRADE_SETUP_PROXY_CONTAINER are both set, and
until the proxy existed the pilot host had neither: dependency installation was
disabled, which is the correct failure and also means no repository work at all.

The proxy is now that route. These pin its SHAPE — an allowlist, a default deny,
no way in from outside. What it actually does to traffic is proved by
`scripts/proxy_egress_check.sh`, which runs the real container against the real
topology and reads squid's own log; the shape is here because a widened
allowlist is a one-line change nobody would otherwise notice in review.
"""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
CONF = ROOT / "docker" / "squid.conf"
DOCKERFILE = ROOT / "docker" / "registry-proxy.Dockerfile"

#: Exactly what the install recipes in pipeline/repo_deps.py need, and nothing
#: else. pypi.org serves the index and .pythonhosted.org the archives (pip, uv,
#: poetry — and `pip install uv "poetry>=2"` is how the last two arrive);
#: .npmjs.org serves npm and pnpm.
ALLOWED = {"pypi.org", ".pythonhosted.org", ".npmjs.org"}


def _compose() -> dict:
    """The prod overlay, parsed.

    `yaml.safe_load` cannot: the overlay uses Compose's own `!reset` tag on the
    api and frontend ports, which is not plain YAML and raises "could not
    determine a constructor". Unknown Compose tags are read as None here — none
    of the assertions below depends on their value.
    """
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    return yaml.load(
        (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"),
        Loader=Loader)


def _directives() -> list[str]:
    return [line.strip() for line in CONF.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")]


def test_the_allowlist_is_exactly_what_the_recipes_need():
    """A host added here widens what a repository's install hooks can reach.
    That is a decision, and this is where it gets made visible."""
    domains = set()
    for line in _directives():
        if line.startswith("acl registries dstdomain"):
            domains.update(line.split("dstdomain", 1)[1].split())

    assert domains == ALLOWED, (
        f"the registry allowlist changed to {sorted(domains)}; if that is"
        " deliberate, say why in squid.conf and update ALLOWED here"
    )


def test_the_last_word_is_deny():
    """🔴 THE LOAD-BEARING LINE. Everything not named above — the metadata
    endpoint, the Docker host, the api and database containers the proxy can
    itself reach on its other network, every arbitrary destination — is refused
    by not being allowed. An `allow` after this would silently undo it."""
    access = [line for line in _directives() if line.startswith("http_access")]

    assert access[-1] == "http_access deny all", access


def test_only_the_registries_are_ever_allowed():
    """One allow rule, naming one acl. A second one is how an allowlist becomes
    decorative."""
    allows = [line for line in _directives()
              if line.startswith("http_access allow")]

    assert allows == ["http_access allow registries"], allows


def test_a_tunnel_may_only_be_opened_to_443():
    """Without this, CONNECT to an allowlisted NAME on any port is permitted,
    which turns a registry allowlist into a hostname check on an arbitrary
    service. `scripts/proxy_egress_check.sh` proves it on pypi.org:8443."""
    assert "http_access deny CONNECT !SSL_ports" in _directives()


def test_squids_own_manager_is_refused():
    """It listens on the same port a build hook is given."""
    assert "http_access deny manager" in _directives()


def test_it_keeps_no_copy_of_what_it_fetched():
    """A cache would hold a team's dependencies between runs. Installs are off
    the chat path already, so the speed is not worth the question."""
    assert "cache deny all" in _directives()


def test_denials_reach_the_container_log():
    """The evidence that the policy is acting rather than a claim that it is."""
    assert any(line.startswith("access_log stdio:") for line in _directives())


def test_the_config_is_parsed_at_build_time():
    """A typo in an ACL is a config squid rejects at startup, and a proxy that
    will not start reads downstream as "dependency install is broken" from
    inside a container nobody is watching. The build fails instead.

    That is not hypothetical: the first version of this image exited with
    `FATAL: failed to open /var/run/squid.pid: (13) Permission denied`, and the
    check script's deny cases all "passed" against a dead proxy because an
    unreachable host and a refused request looked the same to it.
    """
    body = DOCKERFILE.read_text(encoding="utf-8")

    assert "squid -k parse" in body


def test_the_proxy_is_a_service_the_release_manages():
    """An egress policy that exists only in one operator's shell history is one
    the next release deletes."""
    proxy = _compose()["services"]["registry-proxy"]

    assert proxy["build"]["dockerfile"] == "docker/registry-proxy.Dockerfile"
    assert proxy["restart"] == "unless-stopped"
    # The name COMRADE_SETUP_PROXY_CONTAINER has to match, and a generated
    # `comrade-registry-proxy-1` moves with the project name.
    assert proxy["container_name"] == "comrade-registry-proxy"


def test_the_proxy_is_not_reachable_from_outside():
    """It is the way out of a sandbox, not a way in to one."""
    assert "ports" not in _compose()["services"]["registry-proxy"]


def test_setup_still_refuses_to_run_without_the_proxy(monkeypatch):
    """The fail-closed behaviour this replaced nothing about. "Not configured"
    must never quietly mean "setup with the whole internet"."""
    from shared.config import settings
    from agent.sandbox import SandboxError, run_setup

    monkeypatch.setattr(settings, "comrade_setup_proxy_url", "")
    monkeypatch.setattr(settings, "comrade_setup_proxy_container", "")

    with pytest.raises(SandboxError) as refused:
        run_setup(["true"], root=ROOT, deps="comrade-deps-nonexistent")

    assert "COMRADE_SETUP_PROXY_URL" in str(refused.value)
