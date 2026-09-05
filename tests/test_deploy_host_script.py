from pathlib import Path


def test_deploy_script_pins_the_requested_commit_and_waits_for_ready() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "deploy_host.sh").read_text()

    assert 'git checkout --detach --force "$1"' in script
    assert 'docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build' in script
    assert 'http://localhost:8000/ready' in script
