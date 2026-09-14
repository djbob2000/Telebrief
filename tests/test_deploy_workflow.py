from pathlib import Path

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "deploy-dev.yml"


def test_dev_deploy_is_immutable_noninteractive_and_verifies_all_runtime_services():
    text = WORKFLOW.read_text()

    assert "DEPLOY_TAG: ${{ github.event_name == 'workflow_dispatch'" in text
    assert "dev-${{ github.sha }}" in text
    assert 'docker pull "ghcr.io/${repository}:dev"' in text
    assert 'dev_revision="$(docker image inspect "ghcr.io/${repository}:dev"' in text
    assert (
        "docker compose run --rm --interactive=false -T telebrief-app python scripts/migrate.py </dev/null"
        in text
    )
    assert text.index("docker compose stop telebrief-processing-worker") < text.index(
        "docker compose run --rm --interactive=false -T telebrief-app python scripts/migrate.py"
    )
    assert (
        "telebrief-app telebrief-worker telebrief-processing-worker telebrief-authority-worker"
        in text
    )
    assert "docker compose stop telebrief-processing-worker telebrief-authority-worker" in text
    assert "org.opencontainers.image.revision" in text
    assert (
        "repository=\"$(printf '%s' \"$GITHUB_REPOSITORY\" | tr '[:upper:]' '[:lower:]')\"" in text
    )
    assert "SCHEMA_VERSION_MAXIMUM" in text
    assert 'applied_schema="$(printf' in text
    assert 'test "$runtime_schema" = "$applied_schema"' in text
