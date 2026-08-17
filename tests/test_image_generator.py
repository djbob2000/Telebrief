import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.image_generator import NewsImageGenerator


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.gemini_api_key = "test-gemini-key"
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
    generator.ai_provider = mock_provider

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
    mock_config.gemini_api_key = None
    generator = NewsImageGenerator(mock_config, mock_logger)
    generator.ai_provider = None

    prompt = await generator.generate_prompt(
        title="Блэкаут в Бердянске",
        lead="В городе отключили свет.",
        article_text="Подробный текст статьи...",
        city_name="Бердянск",
    )

    assert "Berdyansk" in prompt
    assert "16:9" in prompt


@pytest.mark.asyncio
async def test_generate_image_saves_valid_file(mock_config, mock_logger, tmp_path):
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
async def test_generate_image_handles_quota_gracefully(mock_config, mock_logger, tmp_path):
    generator = NewsImageGenerator(mock_config, mock_logger)
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.text = "Quota exceeded"

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_resp)):
        saved_path = await generator.generate_image(
            prompt="A residential courtyard in Berdyansk",
            output_dir=tmp_path,
        )

    assert saved_path is None
    mock_logger.warning.assert_called()
