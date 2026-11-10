# ruff: noqa: S101
"""A failed Gate batch records its HTTP status and a short, token-free message."""

from __future__ import annotations

from types import SimpleNamespace

from src.processing.event_triage import _TRIAGE_ERROR_MESSAGE_LIMIT, _triage_error_detail


class _ProviderError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_status_and_class_come_from_the_provider_error() -> None:
    status, kind, message = _triage_error_detail(
        _ProviderError("Insufficient credits", status_code=402)
    )

    assert status == 402
    assert kind == "_ProviderError"
    assert message == "Insufficient credits"


def test_status_falls_back_to_the_response_of_a_transport_error() -> None:
    exc = RuntimeError("upstream refused")
    exc.response = SimpleNamespace(status_code=429)  # type: ignore[attr-defined]

    status, _kind, _message = _triage_error_detail(exc)

    assert status == 429


def test_error_without_an_http_status_stores_none() -> None:
    status, kind, _message = _triage_error_detail(ValueError("gate response missing results list"))

    assert status is None
    assert kind == "ValueError"


def test_keys_are_redacted_and_the_message_is_bounded() -> None:
    leaked = "sk-or-v1-abcdef1234567890"
    exc = _ProviderError(f"bad key {leaked} " + "x" * 1000, status_code=401)

    _status, _kind, message = _triage_error_detail(exc)

    assert leaked not in message
    assert "[redacted]" in message
    assert len(message) <= _TRIAGE_ERROR_MESSAGE_LIMIT


def test_bearer_token_is_redacted() -> None:
    exc = _ProviderError("Authorization: Bearer abc.def.ghi failed", status_code=403)

    _status, _kind, message = _triage_error_detail(exc)

    assert "abc.def.ghi" not in message
    assert "[redacted]" in message
