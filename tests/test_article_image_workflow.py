from unittest.mock import AsyncMock, patch

import pytest

from src.core import generate_and_publish_article


@pytest.mark.asyncio
async def test_generate_and_publish_article_with_image(sample_config, mock_logger, tmp_path):
    sample_config.settings.article.enabled = True
    sample_config.settings.article.fallback_save_dir = str(tmp_path)
    sample_config.settings.article.telegraph_access_token = "mock-telegraph-token"
    sample_config.settings.article.author_name = "@test_author"
    sample_config.settings.target_chat_id = "@test_chat"

    fake_messages = {"@channel1": [AsyncMock()]}

    with (
        patch("src.core._collect_messages", new=AsyncMock(return_value=fake_messages)),
        patch(
            "src.article_generator.ArticleGenerator.generate_article",
            new=AsyncMock(return_value=("Тестовый заголовок", "Тестовый лид", "Текст статьи...")),
        ),
        patch(
            "src.telegraph.TelegraphPublisher.create_page",
            new=AsyncMock(return_value="https://telegra.ph/mock-url"),
        ),
        patch(
            "src.image_generator.NewsImageGenerator.generate_prompt",
            new=AsyncMock(return_value="mock prompt"),
        ),
        patch(
            "src.image_generator.NewsImageGenerator.generate_image",
            new=AsyncMock(return_value=tmp_path / "mock.jpg"),
        ),
        patch(
            "src.sender.DigestSender.send_article_with_photo", new=AsyncMock(return_value=True)
        ) as mock_send,
    ):
        success = await generate_and_publish_article(sample_config, mock_logger, dry_run=False)

        assert success is True
        assert mock_send.called
        kwargs = mock_send.call_args.kwargs
        assert kwargs["title"] == "Тестовый заголовок"
        assert kwargs["telegraph_url"] == "https://telegra.ph/mock-url"
        assert kwargs["photo_path"] == tmp_path / "mock.jpg"


@pytest.mark.asyncio
async def test_generate_and_publish_article_dry_run(sample_config, mock_logger, tmp_path):
    sample_config.settings.article.enabled = True
    sample_config.settings.article.fallback_save_dir = str(tmp_path)

    fake_messages = {"@channel1": [AsyncMock()]}

    with (
        patch("src.core._collect_messages", new=AsyncMock(return_value=fake_messages)),
        patch(
            "src.article_generator.ArticleGenerator.generate_article",
            new=AsyncMock(return_value=("Тестовый заголовок", "Тестовый лид", "Текст статьи...")),
        ),
        patch(
            "src.image_generator.NewsImageGenerator.generate_prompt",
            new=AsyncMock(return_value="mock prompt"),
        ),
        patch(
            "src.image_generator.NewsImageGenerator.generate_image",
            new=AsyncMock(return_value=tmp_path / "mock.jpg"),
        ),
    ):
        success = await generate_and_publish_article(sample_config, mock_logger, dry_run=True)

        assert success is True
