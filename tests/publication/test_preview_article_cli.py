"""CLI routing for frozen replay and the unchanged ordinary preview defaults."""

from __future__ import annotations

import datetime as dt
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from scripts import preview_article


@pytest.mark.unit
def test_run_id_rejects_explicit_snapshot_overrides():
    with pytest.raises(SystemExit):
        preview_article.parse_args(["--run-id", "189", "--date", "2026-09-23"])
    with pytest.raises(SystemExit):
        preview_article.parse_args(["--run-id", "189", "--hours", "24"])


@pytest.mark.unit
def test_run_id_arguments_leave_edition_implicit_and_hours_unspecified():
    args = preview_article.parse_args(["--run-id", "189"])
    assert args.run_id == 189
    assert args.edition is None
    assert args.hours is None
    assert args.date is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_frozen_run_cli_writes_article_and_safe_metadata_without_facade(
    monkeypatch, tmp_path
):
    article_path = tmp_path / "article.md"
    diagnostics_path = tmp_path / "nested" / "diagnostics.json"
    preview = SimpleNamespace(
        edition_slug="berdyansk",
        markdown="# Город\n\nИстория.\n",
        diagnostics={"schema_version": "article-run-preview-v1", "run_id": 189},
    )
    calls = []

    class Infra:
        async def close(self):
            calls.append("closed")

    async def fake_build_infrastructure(_database):
        return Infra()

    async def frozen_preview(run_id, *, config, expected_edition_slug=None):
        calls.append(("frozen", run_id, config, expected_edition_slug))
        return preview

    def unexpected_facade(*args, **kwargs):
        raise AssertionError("frozen replay must not call the new-snapshot facade")

    args = preview_article.parse_args(
        [
            "--run-id",
            "189",
            "--output",
            str(article_path),
            "--diagnostics-output",
            str(diagnostics_path),
        ]
    )
    monkeypatch.setattr(preview_article, "parse_args", lambda: args)
    config = SimpleNamespace(database=object(), settings=SimpleNamespace(timezone="UTC"))
    monkeypatch.setattr(preview_article, "load_config", lambda: config)
    monkeypatch.setattr(preview_article, "build_infrastructure", fake_build_infrastructure)
    monkeypatch.setattr(preview_article, "install_runtime", lambda _infra: None)
    monkeypatch.setattr(preview_article, "build_article_preview_from_run", frozen_preview)
    monkeypatch.setattr(preview_article, "build_publication_preview", unexpected_facade)

    await preview_article.main()

    assert calls[0] == ("frozen", 189, config, None)
    assert calls[-1] == "closed"
    assert article_path.read_text(encoding="utf-8") == "# Город\n\nИстория.\n"
    diagnostics_text = diagnostics_path.read_text(encoding="utf-8")
    assert json.loads(diagnostics_text) == preview.diagnostics
    assert "История" not in diagnostics_text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_diagnostics_status_does_not_pollute_markdown_stdout(monkeypatch, tmp_path, capsys):
    diagnostics_path = tmp_path / "diagnostics.json"

    class Infra:
        async def close(self):
            return None

    async def fake_build_infrastructure(_database):
        return Infra()

    async def frozen_preview(_run_id, *, config, expected_edition_slug=None):
        return SimpleNamespace(
            edition_slug="berdyansk",
            markdown="# Вечер\n\nТекст.\n",
            diagnostics={"schema_version": "article-run-preview-v1"},
        )

    args = preview_article.parse_args(
        ["--run-id", "189", "--diagnostics-output", str(diagnostics_path)]
    )
    monkeypatch.setattr(preview_article, "parse_args", lambda: args)
    monkeypatch.setattr(
        preview_article,
        "load_config",
        lambda: SimpleNamespace(database=object(), settings=SimpleNamespace(timezone="UTC")),
    )
    monkeypatch.setattr(preview_article, "build_infrastructure", fake_build_infrastructure)
    monkeypatch.setattr(preview_article, "install_runtime", lambda _infra: None)
    monkeypatch.setattr(preview_article, "build_article_preview_from_run", frozen_preview)

    await preview_article.main()

    captured = capsys.readouterr()
    assert captured.out == "# Вечер\n\nТекст.\n"
    assert "Diagnostics saved" in captured.err


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_conflicting_edition_is_rejected(monkeypatch):
    class Infra:
        async def close(self):
            return None

    async def fake_build_infrastructure(_database):
        return Infra()

    async def frozen_preview(_run_id, *, config, expected_edition_slug=None):
        if expected_edition_slug != "berdyansk":
            raise ValueError("requested edition conflicts with frozen run edition")
        return SimpleNamespace(edition_slug="berdyansk", markdown="# Город\n", diagnostics={})

    args = preview_article.parse_args(["--run-id", "189", "--edition", "melitopol"])
    monkeypatch.setattr(preview_article, "parse_args", lambda: args)
    monkeypatch.setattr(
        preview_article,
        "load_config",
        lambda: SimpleNamespace(database=object(), settings=SimpleNamespace(timezone="UTC")),
    )
    monkeypatch.setattr(preview_article, "build_infrastructure", fake_build_infrastructure)
    monkeypatch.setattr(preview_article, "install_runtime", lambda _infra: None)
    monkeypatch.setattr(preview_article, "build_article_preview_from_run", frozen_preview)

    with pytest.raises(ValueError, match="conflicts with frozen run edition"):
        await preview_article.main()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_cli_keeps_berdyansk_24_hour_new_snapshot_preview(monkeypatch, capsys):
    class Infra:
        async def close(self):
            return None

    async def fake_build_infrastructure(_database):
        return Infra()

    calls = []

    async def new_snapshot_preview(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            title="Вечерний выпуск",
            lead="Подтверждённый лид.",
            body="Основной текст.",
        )

    args = preview_article.parse_args([])
    monkeypatch.setattr(preview_article, "parse_args", lambda: args)
    monkeypatch.setattr(
        preview_article,
        "load_config",
        lambda: SimpleNamespace(database=object(), settings=SimpleNamespace(timezone="UTC")),
    )
    monkeypatch.setattr(preview_article, "build_infrastructure", fake_build_infrastructure)
    monkeypatch.setattr(preview_article, "install_runtime", lambda _infra: None)
    monkeypatch.setattr(preview_article, "build_publication_preview", new_snapshot_preview)

    await preview_article.main()

    assert calls[0]["edition_slug"] == "berdyansk"
    assert calls[0]["lookback_hours"] == 24
    assert calls[0]["snapshot_at"] is None
    assert (
        capsys.readouterr().out == "# Вечерний выпуск\n\nПодтверждённый лид.\n\nОсновной текст.\n"
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_timezone", "expected_timezone"),
    [
        ("Europe/Kyiv", "Europe/Kyiv"),
        ("Europe/Zaporozhye", "Europe/Kyiv"),
        ("Europe/Kiev", "Europe/Kyiv"),
    ],
)
async def test_explicit_date_uses_selected_edition_timezone(
    monkeypatch, capsys, stored_timezone, expected_timezone
):
    calls = []

    class Connection:
        async def execute(self, query, params):
            assert query == "SELECT timezone FROM editions WHERE slug = %s"
            assert params == ("local-edition",)
            return SimpleNamespace(fetchone=self.fetchone)

        async def fetchone(self):
            return (stored_timezone,)

    class UnitOfWork:
        @asynccontextmanager
        async def transaction(self):
            yield Connection()

    class Infra:
        uow = UnitOfWork()

        async def close(self):
            return None

    async def fake_build_infrastructure(_database):
        return Infra()

    async def new_snapshot_preview(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(title="Вечер", lead="", body="Текст.")

    args = preview_article.parse_args(["--edition", "local-edition", "--date", "2026-09-23"])
    monkeypatch.setattr(preview_article, "parse_args", lambda: args)
    monkeypatch.setattr(
        preview_article,
        "load_config",
        lambda: SimpleNamespace(database=object(), settings=SimpleNamespace(timezone="UTC")),
    )
    monkeypatch.setattr(preview_article, "build_infrastructure", fake_build_infrastructure)
    monkeypatch.setattr(preview_article, "install_runtime", lambda _infra: None)
    monkeypatch.setattr(preview_article, "build_publication_preview", new_snapshot_preview)

    await preview_article.main()

    snapshot_at = calls[0]["snapshot_at"]
    assert snapshot_at.tzinfo == ZoneInfo(expected_timezone)
    assert snapshot_at.astimezone(dt.timezone.utc) == dt.datetime(
        2026, 9, 23, 17, 0, tzinfo=dt.timezone.utc
    )
    assert calls[0]["edition_slug"] == "local-edition"
    assert capsys.readouterr().out == "# Вечер\n\nТекст.\n"
