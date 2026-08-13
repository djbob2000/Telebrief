"""
Editorial article generator adhering to news-style local journalism rules.
"""

import html
import logging
from pathlib import Path
from typing import Dict, List, Tuple

from src.ai_providers import AIProvider, create_provider
from src.collector import Message
from src.config_loader import Config
from src.xml_escape import escape_xml_delimiters

_DEFAULT_ARTICLE_PROMPT_PATH = str(Path(__file__).parent / "prompts" / "article_news_style.txt")


class ArticleGenerator:
    """Generates extended editorial news articles from raw Telegram messages."""

    def __init__(self, config: Config, logger: logging.Logger):
        """Initialize article generator.

        Args:
            config: Application configuration.
            logger: Logger instance.
        """
        self.config = config
        self.logger = logger
        self.provider: AIProvider = create_provider(
            provider_name=config.settings.ai_provider,
            logger=logger,
            openai_api_key=config.openai_api_key,
            openai_base_url=config.openai_base_url,
            anthropic_api_key=config.anthropic_api_key,
            google_api_key=config.google_api_key,
            ollama_base_url=config.settings.ollama_base_url,
            api_timeout=config.settings.api_timeout,
        )
        self.model = config.settings.ai_model
        self.temperature = 0.4
        self.max_tokens = 32000
        self.output_language = config.settings.output_language

        prompt_path = Path(_DEFAULT_ARTICLE_PROMPT_PATH)
        if not prompt_path.exists():
            raise FileNotFoundError(f"Article prompt template not found: {prompt_path}")
        self.base_prompt_template = prompt_path.read_text(encoding="utf-8")

    def _format_messages_for_prompt(
        self, messages_by_channel: Dict[str, List[Message]], max_chars: int = 150000
    ) -> str:
        """Format all channel messages into an XML structure.

        Args:
            messages_by_channel: Map of channel names to message lists.
            max_chars: Maximum character limit for aggregated messages.

        Returns:
            Formatted XML string with all channel messages.
        """
        formatted_channels: List[str] = []
        for channel_name, messages in messages_by_channel.items():
            if not messages:
                continue
            lines: List[str] = []
            for i, msg in enumerate(messages, 1):
                timestamp = msg.timestamp.strftime("%H:%M")
                text = msg.text.replace("\r", " ").replace("\n", " ").replace(" | ", " - ")
                sender = msg.sender.replace("\r", " ").replace("\n", " ").replace(" | ", " - ")
                link = msg.link if msg.link and msg.link != "#" else ""
                link_part = f" | {link}" if link else ""
                lines.append(f"{i}. [{timestamp}] {sender}: {text}{link_part}")

            safe_name = html.escape(channel_name, quote=True)
            escaped_content = escape_xml_delimiters("\n".join(lines))
            tag_open = f'<channel_messages channel="{safe_name}" total="{len(lines)}">'
            formatted_channels.append(f"{tag_open}\n{escaped_content}\n</channel_messages>")

        joined = "\n\n".join(formatted_channels)
        return joined[:max_chars]

    @staticmethod
    def _parse_article_response(text: str) -> Tuple[str, str, str]:
        """Extract title, lead, and clean markdown body from AI output.

        Args:
            text: Raw AI completion text.

        Returns:
            Tuple of (title, lead, markdown_body).
        """
        lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
        title = "Редакционная картина дня в Бердянске"
        lead = ""

        found_title = False
        for line in lines:
            if line.startswith("# ") and not found_title:
                title = line[2:].strip()
                found_title = True
            elif found_title and not lead and not line.startswith("#"):
                lead = line

        body = text.strip()
        return title, lead, body

    async def generate_article(
        self, messages_by_channel: Dict[str, List[Message]]
    ) -> Tuple[str, str, str]:
        """Generate long-form article from channel messages.

        Args:
            messages_by_channel: Map of channel names to message lists.

        Returns:
            Tuple of (title, lead, markdown_body).

        Raises:
            ValueError: If messages dictionary contains no messages.
        """
        total_msgs = sum(len(msgs) for msgs in messages_by_channel.values())
        if total_msgs == 0:
            raise ValueError("No messages provided for article generation")

        system_prompt = self.base_prompt_template.format(language=self.output_language)
        formatted_messages = self._format_messages_for_prompt(messages_by_channel)

        user_prompt = f"""Сформируй полную редакционную статью-картину дня по мотивам сообщений ниже.

<all_channel_data total_messages="{total_msgs}">
{formatted_messages}
</all_channel_data>
"""
        chat_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        self.logger.info(f"Generating editorial article from {total_msgs} messages")
        response = await self.provider.chat_completion(
            messages=chat_messages,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort="high",
            thinking=True,
        )
        return self._parse_article_response(response)
