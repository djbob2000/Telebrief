"""Tests for Telegraph publisher and Markdown to DOM converter."""

# pylint: disable=import-error

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.telegraph import TelegraphPublisher, markdown_to_telegraph_nodes


@pytest.mark.unit
def test_markdown_to_telegraph_nodes_headers_and_paragraphs():
    """Markdown headers, paragraphs, bold, italic, and links convert to Telegraph DOM."""
    md = """# Заголовок 1
Вводный абзац с **жирным текстом**, *курсивом* и [ссылкой на канал](https://t.me/berdiansk_me).

## Раздел 2
Второй абзац с цитатой:
> «Как мы сообщали ранее, ремонтные работы завершены»

- Пункт списка 1
- Пункт списка 2
"""
    nodes = markdown_to_telegraph_nodes(md)
    assert len(nodes) >= 4

    # Check tags
    tags = [n.get("tag") for n in nodes if isinstance(n, dict)]
    assert "h3" in tags
    assert "p" in tags
    assert "blockquote" in tags
    assert "ul" in tags

    # Check inline link inside first paragraph
    para_node = next(n for n in nodes if n.get("tag") == "p")
    children = para_node.get("children", [])
    has_link = any(isinstance(c, dict) and c.get("tag") == "a" for c in children)
    assert has_link

    # Check ul contains li
    ul_node = next(n for n in nodes if n.get("tag") == "ul")
    li_children = ul_node.get("children", [])
    assert len(li_children) == 2
    assert all(isinstance(li, dict) and li.get("tag") == "li" for li in li_children)


@pytest.mark.unit
def test_markdown_to_telegraph_nodes_subheaders_and_empty():
    """H3/H4 headers convert to h4 tag in Telegraph DOM, and empty text returns empty list."""
    md = """### Подзаголовок 3
#### Подзаголовок 4
Текст параграфа.
"""
    nodes = markdown_to_telegraph_nodes(md)
    tags = [n.get("tag") for n in nodes if isinstance(n, dict)]
    assert tags == ["h4", "h4", "p"]
    assert markdown_to_telegraph_nodes("") == []
    assert markdown_to_telegraph_nodes("   \n\n  ") == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_telegraph_publisher_create_page_success():
    """TelegraphPublisher creates page via createPage API call and returns page URL."""
    publisher = TelegraphPublisher(access_token="test_token_123")

    mock_response = {
        "ok": True,
        "result": {
            "path": "Sample-Article-08-14",
            "url": "https://telegra.ph/Sample-Article-08-14",
            "title": "Sample Article",
        },
    }

    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_resp_obj = AsyncMock()
        mock_resp_obj.status = 200
        mock_resp_obj.json = AsyncMock(return_value=mock_response)
        mock_post.return_value.__aenter__.return_value = mock_resp_obj

        url = await publisher.create_page(
            title="Sample Article",
            content_markdown="# Sample Article\n\nContent paragraph.",
            author_name="Бердянск Новости",
        )
        assert url == "https://telegra.ph/Sample-Article-08-14"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_telegraph_publisher_token_cache(tmp_path):
    """TelegraphPublisher caches generated access token to file and reuses it."""
    token_file = tmp_path / "telegraph_token.json"
    publisher = TelegraphPublisher(access_token=None, token_file=token_file)

    # First call: creates account via API
    mock_account_response = {
        "ok": True,
        "result": {
            "short_name": "Telebrief",
            "author_name": "Бердянск Новости",
            "access_token": "newly_created_token_abc",
        },
    }

    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_resp_obj = AsyncMock()
        mock_resp_obj.status = 200
        mock_resp_obj.json = AsyncMock(return_value=mock_account_response)
        mock_post.return_value.__aenter__.return_value = mock_resp_obj

        token = await publisher.get_or_create_access_token()
        assert token == "newly_created_token_abc"
        assert token_file.exists()
        saved = json.loads(token_file.read_text(encoding="utf-8"))
        assert saved.get("access_token") == "newly_created_token_abc"

    # Second publisher instance: reads from token_file without API call
    publisher2 = TelegraphPublisher(access_token=None, token_file=token_file)
    token2 = await publisher2.get_or_create_access_token()
    assert token2 == "newly_created_token_abc"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_telegraph_publisher_api_error_raises():
    """TelegraphPublisher raises RuntimeError if Telegraph API returns ok=False."""
    publisher = TelegraphPublisher(access_token="test_token_123")

    mock_error_response = {
        "ok": False,
        "error": "CONTENT_FORMAT_INVALID",
    }

    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_resp_obj = AsyncMock()
        mock_resp_obj.status = 200
        mock_resp_obj.json = AsyncMock(return_value=mock_error_response)
        mock_post.return_value.__aenter__.return_value = mock_resp_obj

        with pytest.raises(
            RuntimeError, match="Telegraph createPage failed: CONTENT_FORMAT_INVALID"
        ):
            await publisher.create_page(
                title="Bad Article",
                content_markdown="Some content",
            )
