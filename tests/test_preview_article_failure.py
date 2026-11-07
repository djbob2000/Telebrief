# ruff: noqa: S101
"""Ordinary article preview must leave a labelled file and a nonzero exit on failure."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "preview_article.py"


def _load_preview_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("preview_article_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeInfra:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _patch_infrastructure(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> _FakeInfra:
    infra = _FakeInfra()

    async def fake_build_infrastructure(_database_config: object) -> _FakeInfra:
        return infra

    monkeypatch.setattr(module, "load_config", lambda: SimpleNamespace(database=object()))
    monkeypatch.setattr(module, "build_infrastructure", fake_build_infrastructure)
    monkeypatch.setattr(module, "install_runtime", lambda _infra: None)
    return infra


def test_failed_ordinary_preview_writes_labelled_output_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_preview_script()
    infra = _patch_infrastructure(monkeypatch, module)
    leaked_text = "SOURCE-TEXT-THAT-MUST-NOT-LEAK"

    async def failing_preview(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError(leaked_text)

    monkeypatch.setattr(module, "_ordinary_preview_markdown", failing_preview)
    output = tmp_path / "preview.md"
    monkeypatch.setattr(
        sys,
        "argv",
        ["preview_article.py", "--type", "monthly_article", "--output", str(output)],
    )

    with pytest.raises(SystemExit) as exit_info:
        asyncio.run(module.main())

    assert exit_info.value.code == 1
    assert infra.closed is True
    written = output.read_text(encoding="utf-8")
    assert written.startswith("# FAILED PREVIEW — DO NOT PUBLISH")
    assert "monthly_article" in written
    assert leaked_text not in written
    assert "RuntimeError" in written
    captured = capsys.readouterr()
    assert leaked_text not in captured.err
    assert "Preview failed: RuntimeError" in captured.err


def test_successful_ordinary_preview_writes_article_without_failure_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_preview_script()
    infra = _patch_infrastructure(monkeypatch, module)

    async def ok_preview(*_args: object, **_kwargs: object) -> str:
        return "# Заголовок\n\nТекст статьи.\n"

    monkeypatch.setattr(module, "_ordinary_preview_markdown", ok_preview)
    output = tmp_path / "ok.md"
    monkeypatch.setattr(sys, "argv", ["preview_article.py", "--output", str(output)])

    asyncio.run(module.main())

    assert infra.closed is True
    assert output.read_text(encoding="utf-8") == "# Заголовок\n\nТекст статьи.\n"


def test_failed_preview_markdown_never_contains_exception_message(tmp_path: Path) -> None:
    module = _load_preview_script()
    args = module.parse_args(["--type", "daily_article", "--date", "2026-11-06"])
    text = module._failed_preview_markdown(args, "berdyansk", ValueError("private detail"))

    assert "FAILED PREVIEW — DO NOT PUBLISH" in text
    assert "2026-11-06" in text
    assert "ValueError" in text
    assert "private detail" not in text
