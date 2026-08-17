import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.image_generator import NewsImageGenerator, _extract_openrouter_image_bytes


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.google_api_key = "test-gemini-key-1"
    config.google_api_key_2 = "test-gemini-key-2"
    config.google_api_key_3 = ""
    config.openrouter_api_key = "test-openrouter-key"
    config.openrouter_base_url = "https://openrouter.ai/api/v1"
    config.openrouter_model = "openrouter/free"
    config.openrouter_image_model = "google/gemini-3.1-flash-lite-image"
    config.settings.ai_provider = "google"
    config.settings.ai_model = "gemini-3.7-flash"
    config.settings.article.fallback_save_dir = "data/articles"
    return config


@pytest.fixture
def mock_logger():
    return MagicMock()


@pytest.mark.asyncio
async def test_generate_prompt_formats_correctly(mock_config, mock_logger):
    generator = NewsImageGenerator(mock_config, mock_logger)
    mock_provider = AsyncMock()
    mock_provider.chat_completion.return_value = (
        "Realistic editorial photojournalism, documentary street photography. "
        "A residential courtyard in Berdyansk, Ukraine during a blackout. "
        "STRICTLY NO text, NO letters. 16:9 aspect ratio."
    )
    generator.prompt_providers = [("test-slot", mock_provider, "gemini-3.7-flash")]

    prompt = await generator.generate_prompt(
        title="Блэкаут в Бердянске",
        lead="В городе отключили свет и воду.",
        article_text="Подробный текст статьи...",
        city_name="Бердянск",
    )

    assert "Berdyansk" in prompt or "blackout" in prompt
    assert mock_provider.chat_completion.called


@pytest.mark.asyncio
async def test_generate_prompt_fallback_when_no_provider(mock_config, mock_logger):
    mock_config.google_api_key = ""
    mock_config.google_api_key_2 = ""
    mock_config.openrouter_api_key = ""
    generator = NewsImageGenerator(mock_config, mock_logger)

    prompt = await generator.generate_prompt(
        title="Блэкаут в Бердянске",
        lead="В городе отключили свет.",
        article_text="Подробный текст статьи...",
        city_name="Бердянск",
    )

    assert "Berdyansk" in prompt
    assert "16:9" in prompt


@pytest.mark.asyncio
async def test_generate_image_tier1_google_success(mock_config, mock_logger, tmp_path):
    generator = NewsImageGenerator(mock_config, mock_logger)
    fake_png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    fake_b64 = base64.b64encode(fake_png_bytes).decode("utf-8")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "image/jpeg",
                                "data": fake_b64,
                            }
                        }
                    ]
                }
            }
        ]
    }

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_resp)):
        saved_path = await generator.generate_image(
            prompt="A residential courtyard in Berdyansk",
            output_dir=tmp_path,
        )

    assert saved_path is not None
    assert saved_path.exists()
    assert saved_path.read_bytes() == fake_png_bytes


@pytest.mark.asyncio
async def test_generate_image_tier2_google_backup_success(mock_config, mock_logger, tmp_path):
    generator = NewsImageGenerator(mock_config, mock_logger)
    fake_png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR_TIER2"
    fake_b64 = base64.b64encode(fake_png_bytes).decode("utf-8")

    fail_resp = MagicMock()
    fail_resp.status_code = 429
    fail_resp.text = "Quota exceeded"

    success_resp = MagicMock()
    success_resp.status_code = 200
    success_resp.json.return_value = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "image/jpeg",
                                "data": fake_b64,
                            }
                        }
                    ]
                }
            }
        ]
    }

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=[fail_resp, success_resp])):
        saved_path = await generator.generate_image(
            prompt="A residential courtyard in Berdyansk",
            output_dir=tmp_path,
        )

    assert saved_path is not None
    assert saved_path.exists()
    assert saved_path.read_bytes() == fake_png_bytes


@pytest.mark.asyncio
async def test_generate_image_tier3_openrouter_success(mock_config, mock_logger, tmp_path):
    generator = NewsImageGenerator(mock_config, mock_logger)
    fake_jpg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF_TIER3"
    fake_b64 = base64.b64encode(fake_jpg_bytes).decode("utf-8")

    fail_google1 = MagicMock()
    fail_google1.status_code = 429
    fail_google1.text = "Quota exceeded 1"

    fail_google2 = MagicMock()
    fail_google2.status_code = 429
    fail_google2.text = "Quota exceeded 2"

    openrouter_resp = MagicMock()
    openrouter_resp.status_code = 200
    openrouter_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "images": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{fake_b64}"},
                        }
                    ],
                }
            }
        ]
    }

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(side_effect=[fail_google1, fail_google2, openrouter_resp]),
    ):
        saved_path = await generator.generate_image(
            prompt="A residential courtyard in Berdyansk",
            output_dir=tmp_path,
        )

    assert saved_path is not None
    assert saved_path.exists()
    assert saved_path.read_bytes() == fake_jpg_bytes


@pytest.mark.asyncio
async def test_generate_image_all_fail_returns_none(mock_config, mock_logger, tmp_path):
    generator = NewsImageGenerator(mock_config, mock_logger)

    fail_resp = MagicMock()
    fail_resp.status_code = 429
    fail_resp.text = "Quota exceeded"

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fail_resp)):
        saved_path = await generator.generate_image(
            prompt="A residential courtyard in Berdyansk",
            output_dir=tmp_path,
        )

    assert saved_path is None
    mock_logger.warning.assert_called()


def test_extract_openrouter_image_bytes_formats():
    fake_bytes = b"sample_image_data"
    fake_b64 = base64.b64encode(fake_bytes).decode("utf-8")

    # Format 1: images list with image_url dict
    data1 = {
        "choices": [
            {"message": {"images": [{"image_url": {"url": f"data:image/jpeg;base64,{fake_b64}"}}]}}
        ]
    }
    assert _extract_openrouter_image_bytes(data1) == fake_bytes

    # Format 2: images list with direct base64 string
    data2 = {"choices": [{"message": {"images": [fake_b64]}}]}
    assert _extract_openrouter_image_bytes(data2) == fake_bytes

    # Format 3: message content with data URI
    data3 = {"choices": [{"message": {"content": f"data:image/jpeg;base64,{fake_b64}"}}]}
    assert _extract_openrouter_image_bytes(data3) == fake_bytes

    # Format 4: empty or invalid
    assert _extract_openrouter_image_bytes({}) is None
    assert _extract_openrouter_image_bytes({"choices": []}) is None
