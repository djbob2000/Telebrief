from pathlib import Path

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "deploy-dev.yml"
CI_WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"


def test_dev_push_uses_one_sequential_verify_build_deploy_job():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "  deploy:\n" in workflow
    assert "  test:\n" not in workflow
    assert "  build-and-push:\n" not in workflow
    assert workflow.count("actions/checkout@v6") == 1
    assert "pre-commit run --all-files --show-diff-on-failure" in workflow
    assert "safety==3.7.0" in workflow
    assert "safety check --json" in workflow
    assert "python -m pytest --cov=src" in workflow
    assert "docker/build-push-action@v7" in workflow
    assert "Execute Remote Deployment" in workflow


def test_general_ci_does_not_duplicate_dev_push_checks():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "push:\n    branches: [main, custom]" in workflow
    assert "push:\n" not in workflow.replace("push:\n    branches: [main, custom]", "")


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


def test_dev_deploy_copies_runtime_config_and_verifies_loaded_authority_settings():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "scp -i ~/.ssh/deploy_key" in workflow
    assert "docker-compose.yml config.yaml" in workflow
    assert "CONFIG_SHA256" in workflow
    assert "sha256sum config.yaml" in workflow
    assert "sha256sum /app/config.yaml" in workflow
    assert "background_authority_enabled" in workflow
    assert "authority_shard_count" in workflow
