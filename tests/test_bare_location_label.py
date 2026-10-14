# ruff: noqa: S101
import pytest

from src.processing.noise_detection import classify_text_noise_or_exclusion


@pytest.mark.parametrize("text", ["📍 БРД, район жд", "📍 ул. Морозова 3", "📌 Центр, рынок"])
def test_bare_location_label_is_directory_payload(text: str) -> None:
    assert classify_text_noise_or_exclusion(text, check_length=True) == (True, "directory_payload")


@pytest.mark.parametrize(
    "text", ["📍 На Горе нет света", "📍 Колония: свет дали", "📍 Лиски, взрыв"]
)
def test_pinned_report_survives(text: str) -> None:
    assert classify_text_noise_or_exclusion(text, check_length=False)[1] != "directory_payload"
