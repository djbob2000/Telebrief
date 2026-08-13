"""
Telegra.ph publisher and Markdown to Telegraph DOM converter.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

TELEGRAPH_API_BASE = "https://api.telegra.ph"
DEFAULT_TOKEN_PATH = Path("data/telegraph_token.json")


def _parse_inline(text: str) -> List[Any]:
    """Parse inline bold, italic, and links into Telegraph node format.

    Args:
        text: Raw text string with markdown inline formatting.

    Returns:
        List of strings and Telegraph node dicts.
    """
    pattern = re.compile(r"(\*\*.*?\*\*|\*.*?\*|\[.*?\]\(.*?\))")
    parts = pattern.split(text)
    result: List[Any] = []

    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) >= 4:
            result.append({"tag": "strong", "children": _parse_inline(part[2:-2])})
        elif part.startswith("*") and part.endswith("*") and len(part) >= 2:
            result.append({"tag": "em", "children": _parse_inline(part[1:-1])})
        elif part.startswith("[") and "](" in part and part.endswith(")"):
            bracket_end = part.find("](")
            link_text = part[1:bracket_end]
            url = part[bracket_end + 2 : -1]
            result.append({"tag": "a", "attrs": {"href": url}, "children": [link_text]})
        else:
            result.append(part)
    return result


def _parse_header(line: str) -> Optional[Dict[str, Any]]:
    """Parse header line into Telegraph h3/h4 node."""
    if line.startswith(("# ", "## ")):
        header_text = re.sub(r"^#+\s*", "", line)
        return {"tag": "h3", "children": _parse_inline(header_text)}
    if line.startswith(("### ", "#### ")):
        header_text = re.sub(r"^#+\s*", "", line)
        return {"tag": "h4", "children": _parse_inline(header_text)}
    return None


def _parse_quote(lines: List[str], start_index: int) -> Tuple[Dict[str, Any], int]:
    """Parse consecutive blockquote lines into a blockquote node."""
    quote_lines: List[str] = []
    i = start_index
    while i < len(lines) and (
        lines[i].strip().startswith(">") or (lines[i].strip() and quote_lines)
    ):
        ql = re.sub(r"^>\s*", "", lines[i].strip())
        quote_lines.append(ql)
        i += 1
        if i < len(lines) and not lines[i].strip():
            break
    quote_text = " ".join(quote_lines)
    return {"tag": "blockquote", "children": _parse_inline(quote_text)}, i


def _parse_list(lines: List[str], start_index: int) -> Tuple[Dict[str, Any], int]:
    """Parse list lines into an unordered list node."""
    list_items: List[Dict[str, Any]] = []
    i = start_index
    while i < len(lines) and lines[i].strip().startswith(("- ", "* ", "• ")):
        item_text = re.sub(r"^[-*•]\s*", "", lines[i].strip())
        list_items.append({"tag": "li", "children": _parse_inline(item_text)})
        i += 1
    return {"tag": "ul", "children": list_items}, i


def _parse_paragraph(lines: List[str], start_index: int) -> Tuple[Dict[str, Any], int]:
    """Parse regular paragraph lines into a paragraph node."""
    para_lines: List[str] = []
    i = start_index
    while (
        i < len(lines)
        and lines[i].strip()
        and not lines[i].strip().startswith(("#", ">", "- ", "* ", "• "))
    ):
        para_lines.append(lines[i].strip())
        i += 1
    para_text = " ".join(para_lines)
    return {"tag": "p", "children": _parse_inline(para_text)}, i


def markdown_to_telegraph_nodes(markdown_text: str) -> List[Dict[str, Any]]:
    """Convert Markdown text to Telegraph DOM nodes.

    Args:
        markdown_text: Full markdown article text.

    Returns:
        List of Telegraph DOM Node dicts.
    """
    nodes: List[Dict[str, Any]] = []
    lines = markdown_text.strip().splitlines()
    i = 0

    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        header_node = _parse_header(line)
        if header_node:
            nodes.append(header_node)
            i += 1
            continue

        if line.startswith(">"):
            quote_node, i = _parse_quote(lines, i)
            nodes.append(quote_node)
            continue

        if line.startswith(("- ", "* ", "• ")):
            list_node, i = _parse_list(lines, i)
            nodes.append(list_node)
            continue

        para_node, i = _parse_paragraph(lines, i)
        nodes.append(para_node)

    return nodes


class TelegraphPublisher:
    """Async client for publishing articles to Telegra.ph."""

    def __init__(
        self,
        access_token: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
        token_file: Path = DEFAULT_TOKEN_PATH,
    ):
        """Initialize Telegraph publisher.

        Args:
            access_token: Optional Telegraph access token.
            logger: Logger instance.
            token_file: Path for persisting generated access token.
        """
        self.access_token = access_token
        self.logger = logger or logging.getLogger(__name__)
        self.token_file = token_file

    async def get_or_create_access_token(
        self, short_name: str = "Telebrief", author_name: str = "Бердянск Новости"
    ) -> str:
        """Retrieve cached access token or create a new Telegraph account.

        Args:
            short_name: Account short name.
            author_name: Default author display name.

        Returns:
            Telegraph access token string.
        """
        if self.access_token:
            return self.access_token

        if self.token_file.exists():
            try:
                data = json.loads(self.token_file.read_text(encoding="utf-8"))
                token = data.get("access_token")
                if token:
                    self.access_token = str(token)
                    return self.access_token
            except Exception as e:
                self.logger.warning(f"Failed to read cached Telegraph token: {e}")

        async with aiohttp.ClientSession() as session:
            payload = {
                "short_name": short_name,
                "author_name": author_name,
            }
            async with session.post(f"{TELEGRAPH_API_BASE}/createAccount", json=payload) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    raise RuntimeError(f"Telegraph createAccount failed: {data.get('error')}")
                token = data["result"]["access_token"]
                self.access_token = str(token)
                try:
                    self.token_file.parent.mkdir(parents=True, exist_ok=True)
                    self.token_file.write_text(
                        json.dumps({"access_token": token}), encoding="utf-8"
                    )
                except Exception as e:
                    self.logger.warning(f"Could not persist Telegraph token: {e}")
                return str(token)

    async def create_page(
        self,
        title: str,
        content_markdown: str,
        author_name: str = "Бердянск Новости",
        author_url: Optional[str] = None,
    ) -> str:
        """Publish article to Telegra.ph and return the page URL.

        Args:
            title: Page title.
            content_markdown: Page content in Markdown.
            author_name: Author display name.
            author_url: Optional link for author.

        Returns:
            Published page URL.
        """
        token = await self.get_or_create_access_token(author_name=author_name)
        nodes = markdown_to_telegraph_nodes(content_markdown)

        payload: Dict[str, Any] = {
            "access_token": token,
            "title": title[:256],
            "author_name": author_name,
            "content": json.dumps(nodes),
            "return_content": False,
        }
        if author_url:
            payload["author_url"] = author_url

        async with aiohttp.ClientSession() as session:
            async with session.post(f"{TELEGRAPH_API_BASE}/createPage", data=payload) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    raise RuntimeError(f"Telegraph createPage failed: {data.get('error')}")
                return str(data["result"]["url"])
