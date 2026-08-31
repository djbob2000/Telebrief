"""Tests for formatter module."""

from datetime import datetime, timezone

import pytest

from src.formatter import DigestFormatter
from src.grouper import GroupedPoint


@pytest.fixture
def english_config(sample_config):
    """sample_config with output_language set to English."""
    sample_config.settings.output_language = "English"
    return sample_config


@pytest.mark.unit
def test_formatter_initialization(sample_config, mock_logger):
    """Test formatter initialization."""
    formatter = DigestFormatter(sample_config, mock_logger)

    assert formatter.config == sample_config
    assert formatter.logger == mock_logger
    assert formatter.use_emojis is True
    assert formatter.include_stats is True


@pytest.mark.unit
def test_create_digest(sample_config, mock_logger, sample_messages):
    """Test digest creation."""
    formatter = DigestFormatter(sample_config, mock_logger)

    overview = "Test overview summary"
    channel_summaries = {"Test Channel": "- Test point 1\n- Test point 2"}
    messages_by_channel = {"Test Channel": sample_messages}

    digest = formatter.create_digest(
        overview=overview,
        channel_summaries=channel_summaries,
        messages_by_channel=messages_by_channel,
        hours=24,
    )

    assert "Дайджест" in digest
    assert overview in digest
    assert "Test point 1" in digest
    assert "Test Channel" in digest
    assert "Статистика" in digest


@pytest.mark.unit
def test_create_header(sample_config, mock_logger):
    """Test header creation."""
    formatter = DigestFormatter(sample_config, mock_logger)
    header = formatter._create_header(24)

    assert "Дайджест" in header
    assert "📊" in header  # Emoji should be included


@pytest.mark.unit
def test_pick_emoji_tech(sample_config, mock_logger):
    """Test emoji selection for tech channels."""
    formatter = DigestFormatter(sample_config, mock_logger)

    assert formatter._pick_emoji("Tech News") == "💻"
    assert formatter._pick_emoji("Dev Channel") == "💻"
    assert formatter._pick_emoji("Программирование") == "💻"


@pytest.mark.unit
def test_pick_emoji_crypto(sample_config, mock_logger):
    """Test emoji selection for crypto channels."""
    formatter = DigestFormatter(sample_config, mock_logger)

    assert formatter._pick_emoji("Crypto News") == "💰"
    assert formatter._pick_emoji("Bitcoin Updates") == "💰"


@pytest.mark.unit
def test_pick_emoji_default(sample_config, mock_logger):
    """Test default emoji selection."""
    formatter = DigestFormatter(sample_config, mock_logger)

    assert formatter._pick_emoji("Random Channel") == "📺"


@pytest.mark.unit
def test_create_statistics(sample_config, mock_logger, sample_messages):
    """Test statistics creation."""
    formatter = DigestFormatter(sample_config, mock_logger)

    messages_by_channel = {
        "Channel 1": sample_messages,
        "Channel 2": sample_messages[:2],
    }

    stats = formatter._create_statistics(messages_by_channel, 24)

    assert "Статистика" in stats
    assert "2 каналов" in stats
    assert "5 сообщений обработано" in stats
    assert "UTC" in stats


@pytest.mark.unit
def test_formatter_without_emojis(sample_config, mock_logger, sample_messages):
    """Test formatter with emojis disabled."""
    sample_config.settings.use_emojis = False
    formatter = DigestFormatter(sample_config, mock_logger)

    emoji = formatter._pick_emoji("Tech News")
    assert emoji == "•"  # Should return bullet instead of emoji


@pytest.mark.unit
def test_formatter_without_stats(sample_config, mock_logger, sample_messages):
    """Test formatter with statistics disabled."""
    sample_config.settings.include_statistics = False
    formatter = DigestFormatter(sample_config, mock_logger)

    overview = "Test overview"
    channel_summaries = {"Test Channel": "- Test point"}
    messages_by_channel = {"Test Channel": sample_messages}

    digest = formatter.create_digest(
        overview=overview,
        channel_summaries=channel_summaries,
        messages_by_channel=messages_by_channel,
        hours=24,
    )

    # Stats should not be included
    assert "Статистика" not in digest


# --- Language / output_language tests ---


@pytest.mark.unit
def test_create_header_uses_output_language(english_config, mock_logger):
    """Header uses output_language: English config produces English header."""
    formatter = DigestFormatter(english_config, mock_logger)
    header = formatter._create_header(24)

    assert "Daily Digest" in header
    assert "Дайджест" not in header


@pytest.mark.unit
def test_format_summary_message_uses_output_language(english_config, mock_logger):
    """format_summary_message respects output_language."""
    formatter = DigestFormatter(english_config, mock_logger)
    msg = formatter.format_summary_message(total_channels=3, total_messages=42, hours=24)

    assert "Digest completed" in msg
    assert "Channels processed" in msg
    assert "Total messages" in msg
    assert "Period" in msg
    assert "Дайджест завершён" not in msg
    assert "Обработано каналов" not in msg


@pytest.mark.unit
def test_create_statistics_uses_output_language(english_config, mock_logger, sample_messages):
    """_create_statistics respects output_language."""
    formatter = DigestFormatter(english_config, mock_logger)
    messages_by_channel = {"Ch1": sample_messages, "Ch2": sample_messages[:1]}

    stats = formatter._create_statistics(messages_by_channel, 24)

    assert "Statistics" in stats
    assert "channels" in stats
    assert "messages processed" in stats
    assert "Статистика" not in stats
    assert "каналов" not in stats


@pytest.mark.unit
def test_format_channel_message_stats_uses_output_language(
    english_config, mock_logger, sample_messages
):
    """format_channel_message per-channel stats respect output_language."""
    formatter = DigestFormatter(english_config, mock_logger)
    msg = formatter.format_channel_message("MyChannel", "Summary text", sample_messages, hours=24)

    assert "Messages processed" in msg
    assert "Обработано сообщений" not in msg


@pytest.mark.unit
def test_overview_section_label_uses_output_language(english_config, mock_logger, sample_messages):
    """create_digest overview section label respects output_language."""
    formatter = DigestFormatter(english_config, mock_logger)
    digest = formatter.create_digest(
        overview="Some overview",
        channel_summaries={"Ch": "- point"},
        messages_by_channel={"Ch": sample_messages},
        hours=24,
    )

    assert "Brief Overview" in digest
    assert "Краткий обзор" not in digest


@pytest.mark.unit
def test_truncation_message_uses_output_language(english_config, mock_logger, sample_messages):
    """Truncation suffix in format_channel_message respects output_language."""
    formatter = DigestFormatter(english_config, mock_logger)
    # Long summary ensures total message exceeds 32768 chars and triggers truncation
    long_summary = "word " * 7000
    msg = formatter.format_channel_message("Ch", long_summary, sample_messages, hours=24)

    assert "truncated due to length limit" in msg
    assert "усечено" not in msg


@pytest.mark.unit
def test_format_date_russian_month_names(sample_config, mock_logger):
    """_format_date returns Russian month names when output_language is Russian."""
    from datetime import datetime

    formatter = DigestFormatter(sample_config, mock_logger)  # output_language=Russian
    # February in Russian genitive (used in dates) is "февраля"
    dt = datetime(2026, 2, 22)
    result = formatter._format_date(dt)

    assert "февраля" in result
    assert "February" not in result


@pytest.mark.unit
def test_format_date_english_month_names(english_config, mock_logger):
    """_format_date returns English month names when output_language is English."""
    from datetime import datetime

    formatter = DigestFormatter(english_config, mock_logger)
    dt = datetime(2026, 2, 22)
    result = formatter._format_date(dt)

    assert "February" in result
    assert "Февраль" not in result


@pytest.mark.unit
def test_create_header_russian_month_name(sample_config, mock_logger):
    """_create_header uses localized month name for Russian output_language."""
    formatter = DigestFormatter(sample_config, mock_logger)
    header = formatter._create_header(24)

    # Month names in Russian must not contain English month strings (spot-check Jan-Mar)
    assert "January" not in header
    assert "February" not in header
    assert "March" not in header


# --- Channel section header tests ---


@pytest.mark.unit
def test_channel_section_header_format(sample_config, mock_logger, sample_messages):
    """Channel section header includes channel name and emoji without external URLs."""
    formatter = DigestFormatter(sample_config, mock_logger)
    section = formatter._create_channel_section("Test Channel", "Summary", sample_messages)
    assert "## 📺 Test Channel" in section
    assert "https://t.me" not in section


@pytest.mark.unit
def test_format_channel_message_header_format(sample_config, mock_logger, sample_messages):
    """format_channel_message header includes channel name and date without external URLs."""
    formatter = DigestFormatter(sample_config, mock_logger)
    msg = formatter.format_channel_message("Test Channel", "Summary", sample_messages)
    assert "# 📺 Test Channel" in msg
    assert "https://t.me" not in msg


# --- Group formatter tests ---


@pytest.mark.unit
def test_pick_group_emoji_known_groups(sample_config, mock_logger):
    """_pick_group_emoji returns correct emoji for known group names."""
    formatter = DigestFormatter(sample_config, mock_logger)

    assert formatter._pick_group_emoji("Events") == "🎪"
    assert formatter._pick_group_emoji("event") == "🎪"
    assert formatter._pick_group_emoji("News") == "📰"
    assert formatter._pick_group_emoji("news") == "📰"
    assert formatter._pick_group_emoji("Sport") == "⚽"
    assert formatter._pick_group_emoji("sports") == "⚽"
    assert formatter._pick_group_emoji("Other") == "📌"
    assert formatter._pick_group_emoji("other") == "📌"


@pytest.mark.unit
def test_pick_group_emoji_default(sample_config, mock_logger):
    """_pick_group_emoji returns default emoji for unknown groups."""
    formatter = DigestFormatter(sample_config, mock_logger)
    assert formatter._pick_group_emoji("RandomTopic") == "📌"


@pytest.mark.unit
def test_format_group_digest_russian_compact_single_message(sample_config, mock_logger):
    """Grouped digest uses one compact Telegram-native document."""
    formatter = DigestFormatter(sample_config, mock_logger)
    sections = [
        (
            "Предупреждения",
            [
                GroupedPoint(
                    point="Напряжение 130–150 В вместо 220",
                    source="Бердянск",
                    source_url="https://t.me/berdiansk_me",
                )
            ],
        ),
        (
            "Другое",
            [
                GroupedPoint(point="Отдают котят", source="Бердянск", source_url=""),
                GroupedPoint(
                    point="Снимут квартиру",
                    source="Бердянск",
                    source_url="https://t.me/berdiansk_me",
                ),
            ],
        ),
    ]

    result = formatter.format_group_digest(sections, hours=24)

    assert f"{formatter._ui['daily_digest']} ·" in result
    assert "**Предупреждения**" in result
    assert "**Другое**" in result
    assert "• Напряжение 130–150 В вместо 220" in result
    assert "[↗]" not in result
    assert "• Отдают котят" in result
    assert "📺 Бердянск" not in result
    assert "📺" not in result
    assert "---" not in result
    assert "#" not in result
    assert "пункт" not in result
    assert "24 часа" not in result


@pytest.mark.unit
def test_format_group_digest_omits_empty_sections_and_uses_requested_hours(
    sample_config, mock_logger
):
    """Empty groups disappear and non-24-hour windows stay compact."""
    formatter = DigestFormatter(sample_config, mock_logger)

    result = formatter.format_group_digest(
        [("Новости", []), ("Другое", [GroupedPoint(point="Факт", source="")])],
        hours=12,
    )

    assert "Новости" not in result
    assert "**Другое**" in result
    assert "пункт" not in result
    assert "12 часов" not in result
    assert formatter.format_group_digest([("Новости", [])], hours=24) == ""


@pytest.mark.unit
def test_format_group_digest_replaces_inline_source_url_with_source_link(
    sample_config, mock_logger
):
    """Inline AI-provided URLs and source markers are stripped cleanly."""
    formatter = DigestFormatter(sample_config, mock_logger)
    message_url = "https://t.me/berdiansk_me/123"

    result = formatter.format_group_digest(
        [
            (
                "Новости",
                [
                    GroupedPoint(
                        point=f"Важная новость 🖇️ → {message_url}",
                        source="Бердянск",
                        source_url="https://t.me/berdiansk_me",
                    )
                ],
            )
        ]
    )

    assert "• Важная новость" in result
    assert "[↗]" not in result
    assert "🖇️" not in result
    assert message_url not in result


@pytest.mark.unit
def test_format_group_digest_normalizes_markdown_wrapped_source_link(sample_config, mock_logger):
    """Markdown-wrapped Telegram sources are stripped cleanly."""
    formatter = DigestFormatter(sample_config, mock_logger)
    message_url = "https://t.me/Brd24discord/208708"

    result = formatter.format_group_digest(
        [
            (
                "Другое",
                [
                    GroupedPoint(
                        point=(
                            "Бердянск снова остался без света. "
                            "[t.me/Brd24discord/208708](https://t.me/Brd24discord)"
                        ),
                        source="Бердянск",
                        source_url="https://t.me/Brd24discord",
                    )
                ],
            )
        ]
    )

    assert "• Бердянск снова остался без света." in result
    assert "[t.me/Brd24discord/208708](" not in result
    assert "[↗]" not in result
    assert message_url not in result


@pytest.mark.unit
def test_format_group_digest_removes_source_markers_and_duplicate_bullets(
    sample_config, mock_logger
):
    """Service source markers and repeated list prefixes do not leak into output."""
    formatter = DigestFormatter(sample_config, mock_logger)

    result = formatter.format_group_digest(
        [
            (
                "Новости",
                [
                    GroupedPoint(
                        point="• • 💧 Вода пропадает в Мелитополе 🖇️ 🖇️ 🖇️",
                        source="Бердянск",
                        source_url="https://t.me/berdiansk_me/123",
                    )
                ],
            )
        ]
    )

    assert "• 💧 Вода пропадает в Мелитополе" in result
    assert "🖇️" not in result
    assert "• •" not in result
    assert "[↗]" not in result


@pytest.mark.unit
def test_format_group_digest_removes_standalone_trailing_arrow(sample_config, mock_logger):
    """A standalone AI arrow is removed cleanly."""
    formatter = DigestFormatter(sample_config, mock_logger)

    result = formatter.format_group_digest(
        [
            (
                "Банки и финансы",
                [
                    GroupedPoint(
                        point="🏦 Наличные: банкоматы работают. → ↗",
                        source="Бердянск",
                        source_url="https://t.me/berdiansk_me/123",
                    )
                ],
            )
        ]
    )

    assert "• 🏦 Наличные: банкоматы работают." in result
    assert " →" not in result
    assert "[↗]" not in result


@pytest.mark.unit
def test_format_group_digest_has_one_generic_title(sample_config, mock_logger):
    """The digest title has no morning/evening edition label."""
    formatter = DigestFormatter(sample_config, mock_logger)

    result = formatter.format_group_digest([("Новости", [GroupedPoint(point="Факт", source="")])])

    assert result.startswith(f"{formatter._ui['daily_digest']} · ")
    assert "Вечерний" not in result
    assert "Утренний" not in result


@pytest.mark.unit
def test_format_group_rich_digest_uses_native_headings_and_unordered_lists(
    sample_config, mock_logger
):
    """Rich digest uses Telegram heading and unordered-list blocks."""
    formatter = DigestFormatter(sample_config, mock_logger)

    result = formatter.format_group_rich_digest(
        [
            (
                "Предупреждения",
                [
                    GroupedPoint(
                        point="Напряжение нестабильно",
                        source="Бердянск",
                        source_url="https://t.me/berdiansk_me/123",
                    ),
                    GroupedPoint(point="Воды нет", source="Бердянск", source_url=""),
                ],
            )
        ]
    )

    blocks = result["rich_message"]["blocks"]
    assert blocks[0] == {
        "type": "heading",
        "size": 2,
        "text": f"{formatter._ui['daily_digest']} · "
        + formatter._format_date(datetime.now(timezone.utc)),
    }
    assert blocks[1] == {"type": "heading", "size": 3, "text": "Предупреждения"}
    assert blocks[2]["type"] == "list"
    assert all("value" not in item for item in blocks[2]["items"])
    assert blocks[2]["items"][0]["blocks"][0]["text"] == ["Напряжение нестабильно"]
    assert blocks[2]["items"][1]["blocks"][0]["text"] == ["Воды нет"]


@pytest.mark.unit
def test_format_group_rich_digest_removes_duplicate_markers_and_empty_groups(
    sample_config, mock_logger
):
    """Rich digest keeps cleaned text and omits groups without points."""
    formatter = DigestFormatter(sample_config, mock_logger)

    result = formatter.format_group_rich_digest(
        [
            ("Пустая группа", []),
            (
                "Новости",
                [
                    GroupedPoint(
                        point="• • 💧 Вода пропадает 🖇️ 🖇️ → ↗",
                        source="Бердянск",
                        source_url="https://t.me/berdiansk_me/456",
                    )
                ],
            ),
        ]
    )

    blocks = result["rich_message"]["blocks"]
    assert len(blocks) == 3
    item_text = blocks[2]["items"][0]["blocks"][0]["text"]
    assert item_text == ["💧 Вода пропадает"]
    assert "Пустая группа" not in str(result)


@pytest.mark.unit
def test_split_group_rich_digest_keeps_group_heading_with_its_list(sample_config, mock_logger):
    """Rich splitting never leaves a group heading without its list."""
    formatter = DigestFormatter(sample_config, mock_logger)
    document = {
        "rich_message": {
            "blocks": [
                {"type": "heading", "size": 2, "text": "Дайджест"},
                {"type": "heading", "size": 3, "text": "📌 Первая"},
                {"type": "list", "items": [{"blocks": [{"type": "paragraph", "text": "A"}]}]},
                {"type": "heading", "size": 3, "text": "📌 Вторая"},
                {"type": "list", "items": [{"blocks": [{"type": "paragraph", "text": "B"}]}]},
            ]
        }
    }

    parts = formatter.split_group_rich_digest(document, max_length=180)

    assert len(parts) == 2
    assert [block["type"] for block in parts[0]["rich_message"]["blocks"]] == [
        "heading",
        "heading",
        "list",
    ]
    assert [block["type"] for block in parts[1]["rich_message"]["blocks"]] == [
        "heading",
        "heading",
        "list",
    ]


@pytest.mark.unit
def test_format_group_rich_digest_parses_markdown_bold_subheadings(sample_config, mock_logger):
    """Rich digest converts **bold title** in points into native bold rich text spans."""
    formatter = DigestFormatter(sample_config, mock_logger)

    result = formatter.format_group_rich_digest(
        [
            (
                "Предупреждения",
                [
                    GroupedPoint(
                        point="⚡ **Критические перепады напряжения**: В большинстве районов города",
                        source="Бердянск",
                        source_url="https://t.me/berdiansk_me/123",
                    ),
                    GroupedPoint(
                        point="**Репрессии в Токмаке**: подробности дела",
                        source="Бердянск",
                        source_url="",
                    ),
                ],
            )
        ]
    )

    items = result["rich_message"]["blocks"][2]["items"]
    # First item: "⚡ ", {"type": "bold", "text": "Критические перепады напряжения"}, ": В большинстве районов города"
    first_item_text = items[0]["blocks"][0]["text"]
    assert first_item_text[0] == "⚡ "
    assert first_item_text[1] == {"type": "bold", "text": "Критические перепады напряжения"}
    assert first_item_text[2] == ": В большинстве районов города"
    assert len(first_item_text) == 3

    # Second item starts directly with bold span
    second_item_text = items[1]["blocks"][0]["text"]
    assert second_item_text[0] == {"type": "bold", "text": "Репрессии в Токмаке"}
    assert second_item_text[1] == ": подробности дела"
    assert len(second_item_text) == 2
