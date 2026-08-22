"""Unit tests for the migrate CLI's domain-schema resolution."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATE_SCRIPT = REPO_ROOT / "scripts" / "migrate.py"


def _load_migrate_cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migrate_cli", MIGRATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migrate_cli() -> ModuleType:
    return _load_migrate_cli()


def test_explicit_flag_overrides_config(migrate_cli, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert migrate_cli._resolve_domain_schema("custom") == "custom"


def test_default_reads_domain_schema_from_config(migrate_cli, tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "database:\n  domain_schema: custom\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert migrate_cli._resolve_domain_schema(None) == "custom"


def test_default_falls_back_to_public_without_config(migrate_cli, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert migrate_cli._resolve_domain_schema(None) == "public"
