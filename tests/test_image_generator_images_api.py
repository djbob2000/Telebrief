# ruff: noqa: S101
"""Image-only OpenRouter models (Muse, GPT Image) are served by /api/v1/images."""

from __future__ import annotations

import base64
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from src import image_generator
from src.image_generator import (
    IMAGE_TEXT_LANGUAGE_RULE,
    NewsImageGenerator,
    _extract_images_api_bytes,
    _images_api_payload,
    _requires_images_api,
)

JPEG = b"\xff\xd8\xff\xe0fake-jpeg"
CHAT_404 = (
    '{"error":{"message":"meta/muse-image is an image generation model and cannot be used '
    'with the chat/completions endpoint. Use the /api/v1/images endpoint instead.","code":404}}'
)


def test_images_api_helpers() -> None:
    assert _requires_images_api(404, CHAT_404)
    assert not _requires_images_api(404, '{"error":{"message":"No endpoints found"}}')
    assert not _requires_images_api(200, CHAT_404)

    data = {"data": [{"b64_json": base64.b64encode(JPEG).decode(), "media_type": "image/jpeg"}]}
    assert _extract_images_api_bytes(data) == JPEG
    assert _extract_images_api_bytes({"data": []}) is None

    payload = _images_api_payload("meta/muse-image", "a street", b"ref")
    assert payload["aspect_ratio"] == "16:9"
    assert payload["size"] == "1920x1080"
    assert "size" not in _images_api_payload("openai/gpt-image-2.5-sunburst", "a street", None)
    assert payload["input_references"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "input_references" not in _images_api_payload("meta/muse-image", "a street", None)


class FakeClient:
    calls: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        self.calls.append((url, json))
        request = httpx.Request("POST", url)
        if url.endswith("/chat/completions"):
            return httpx.Response(404, text=CHAT_404, request=request)
        body = {"data": [{"b64_json": base64.b64encode(JPEG).decode()}]}
        return httpx.Response(200, json=body, request=request)


@pytest.mark.asyncio
async def test_image_only_model_falls_through_to_images_api(monkeypatch, tmp_path) -> None:
    FakeClient.calls = []
    monkeypatch.setattr(image_generator.httpx, "AsyncClient", FakeClient)
    config = SimpleNamespace(
        openrouter_api_key="test-key",
        openrouter_base_url="https://openrouter.ai/api/v1",
        openrouter_model="",
        openrouter_image_model="meta/muse-image",
        google_api_keys=[],
        settings=SimpleNamespace(ai_model="m"),
    )
    generator = NewsImageGenerator(config, logging.getLogger("test"))  # type: ignore[arg-type]

    path = await generator.generate_image("a street", output_dir=tmp_path)

    assert path is not None and path.read_bytes() == JPEG
    assert [url.rsplit("/", 1)[1] for url, _ in FakeClient.calls] == ["completions", "images"]
    assert FakeClient.calls[1][1]["model"] == "meta/muse-image"
    assert FakeClient.calls[1][1]["prompt"] == f"a street {IMAGE_TEXT_LANGUAGE_RULE}"

    FakeClient.calls = []
    await generator.generate_image(
        "clean the photo", output_dir=tmp_path, reference_image_bytes=b"r"
    )
    assert FakeClient.calls[1][1]["prompt"] == "clean the photo"
    assert "input_references" in FakeClient.calls[1][1]


def _prompt_config() -> SimpleNamespace:
    return SimpleNamespace(
        openrouter_api_key="test-key",
        openrouter_base_url="https://openrouter.ai/api/v1",
        openrouter_model="deepseek/deepseek-v4.1-flash-1017:floor",
        openrouter_image_model="meta/muse-image",
        google_api_keys=["g1", "g2"],
        settings=SimpleNamespace(ai_model="deepseek/deepseek-v4.1-flash-1017:floor,openai/x"),
    )


def test_prompt_slots_skip_google_without_gemini_model(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    generator = NewsImageGenerator(_prompt_config(), logging.getLogger("test"))  # type: ignore[arg-type]

    assert [(label, model) for label, _, model in generator.prompt_providers] == [
        ("openrouter-text", "deepseek/deepseek-v4.1-flash-1017:floor")
    ]


def test_prompt_slots_use_declared_gemini_model(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.7-flash, gemini-3.1-pro")
    generator = NewsImageGenerator(_prompt_config(), logging.getLogger("test"))  # type: ignore[arg-type]

    assert [(label, model) for label, _, model in generator.prompt_providers] == [
        ("google-1", "gemini-3.7-flash"),
        ("google-2", "gemini-3.7-flash"),
        ("openrouter-text", "deepseek/deepseek-v4.1-flash-1017:floor"),
    ]


class GoogleImageClient(FakeClient):
    async def post(self, url: str, json: dict[str, Any], headers: Any = None) -> httpx.Response:
        self.calls.append((url, json))
        part = {"inlineData": {"data": base64.b64encode(JPEG).decode()}}
        body = {"candidates": [{"content": {"parts": [part]}}]}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))


def _google_only_generator() -> NewsImageGenerator:
    config = _prompt_config()
    config.openrouter_api_key = ""
    return NewsImageGenerator(config, logging.getLogger("test"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_google_image_fallback_skipped_without_declared_model(monkeypatch, tmp_path) -> None:
    GoogleImageClient.calls = []
    monkeypatch.setattr(image_generator.httpx, "AsyncClient", GoogleImageClient)
    monkeypatch.delenv("GEMINI_IMAGE_MODEL", raising=False)

    path = await _google_only_generator().generate_image("a street", output_dir=tmp_path)

    assert path is None
    assert GoogleImageClient.calls == []


@pytest.mark.asyncio
async def test_google_image_fallback_uses_declared_model(monkeypatch, tmp_path) -> None:
    GoogleImageClient.calls = []
    monkeypatch.setattr(image_generator.httpx, "AsyncClient", GoogleImageClient)
    monkeypatch.setenv("GEMINI_IMAGE_MODEL", "google/gemini-3.1-flash-lite-image:floor")

    path = await _google_only_generator().generate_image("a street", output_dir=tmp_path)

    assert path is not None and path.read_bytes() == JPEG
    assert (
        "/models/gemini-3.1-flash-lite-image:generateContent?key=g1"
        in GoogleImageClient.calls[0][0]
    )
