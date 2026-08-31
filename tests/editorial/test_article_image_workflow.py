from unittest.mock import AsyncMock, patch

import pytest

from src.core import ARTICLE_PUBLICATION_TYPE, generate_and_publish_article


@pytest.mark.asyncio
async def test_generate_and_publish_article_requests_publication_without_inline_sending(
    sample_config, mock_logger, tmp_path
):
    """Non-dry-run defers a durable publication; nothing is generated or sent inline."""
    sample_config.settings.article.enabled = True
    sample_config.settings.article.fallback_save_dir = str(tmp_path)
    sample_config.database.enabled = True
    sample_config.settings.persistent_ingestion = True

    requested = {}

    class _FakeResult:
        run_id = 3
        request_key = "on-demand:test"

    async def _fake_request(publication_type, edition_slug="berdyansk", **kwargs):
        requested["type"] = publication_type
        return _FakeResult()

    with (
        patch("src.publication.facade.request_publication", side_effect=_fake_request),
        patch("src.core._collect_messages", new=AsyncMock()) as mock_collect,
        patch(
            "src.article_generator.ArticleGenerator.generate_article",
            new=AsyncMock(),
        ),
        patch(
            "src.telegraph.TelegraphPublisher.create_page",
            new=AsyncMock(),
        ) as mock_page,
        patch("src.sender.DigestSender.send_article_with_photo", new=AsyncMock()) as mock_send,
    ):
        success = await generate_and_publish_article(sample_config, mock_logger, dry_run=False)

        assert success is True
        assert requested["type"] == ARTICLE_PUBLICATION_TYPE
        mock_collect.assert_not_called()
        mock_page.assert_not_called()
        mock_send.assert_not_called()


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
