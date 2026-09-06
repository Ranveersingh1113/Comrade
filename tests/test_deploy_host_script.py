from pathlib import Path


def test_deploy_script_pins_the_requested_commit_and_waits_for_ready() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "deploy_host.sh").read_text()

    assert 'git checkout --detach --force "$1"' in script
    assert 'docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build' in script
    assert 'docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T api python -m shared.migrations' in script
    assert script.index('python -m shared.migrations') < script.index('http://localhost:8000/ready')
    assert 'http://localhost:8000/ready' in script


def test_workflow_runs_the_deploy_script_from_the_requested_commit() -> None:
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "deploy-pilot.yml").read_text()

    assert 'git show \\"$GITHUB_SHA:scripts/deploy_host.sh\\" | sh -s \\"$GITHUB_SHA\\"' in workflow
