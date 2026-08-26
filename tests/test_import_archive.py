"""Tests for archive importer utilities."""

import json
from pathlib import Path

from scripts.import_archive import clean_text, load_records_from_file, parse_datetime


def test_clean_text():
    raw = "<p>Новости <b>Бердянска</b></p><br/><div>Текст с   пробелами</div>"
    cleaned = clean_text(raw)
    assert cleaned == "Новости Бердянска Текст с пробелами"


def test_parse_datetime():
    # ISO
    d1 = parse_datetime("2023-11-20T15:30:00Z")
    assert d1.year == 2023 and d1.month == 11 and d1.day == 20

    # Custom formats
    d2 = parse_datetime("25.04.2024 14:00")
    assert d2.year == 2024 and d2.month == 4 and d2.day == 25

    d3 = parse_datetime("2022-05-10")
    assert d3.year == 2022 and d3.month == 5 and d3.day == 10


def test_load_records_json(tmp_path: Path):
    sample_file = tmp_path / "sample.json"
    data = [
        {"title": "Новость 1", "text": "Текст 1", "date": "2023-01-01"},
        {"title": "Новость 2", "text": "Текст 2", "date": "2023-02-01"},
    ]
    sample_file.write_text(json.dumps(data), encoding="utf-8")

    records = load_records_from_file(sample_file)
    assert len(records) == 2
    assert records[0]["title"] == "Новость 1"


def test_load_records_csv(tmp_path: Path):
    sample_file = tmp_path / "sample.csv"
    sample_file.write_text(
        "title,text,date\n"
        "Статья 1,Содержимое 1,2023-05-01\n"
        "Статья 2,Содержимое 2,2023-06-01\n",
        encoding="utf-8",
    )

    records = load_records_from_file(sample_file)
    assert len(records) == 2
    assert records[1]["title"] == "Статья 2"
