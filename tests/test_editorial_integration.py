"""End-to-end regression coverage for a rich daily editorial bundle."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.article_generator import ArticleGenerator
from src.collector import Message
from src.config_loader import ChannelConfig, Config, Settings
from src.editorial_audit import FactCheckResult
from src.editorial_models import EditorialAnalysis, StoryCard, StoryElement
from src.editorial_writer import ArticleDraft

FIXTURE = Path(__file__).parent / "fixtures" / "editorial_longform_day.json"


def _load_messages() -> dict[str, list[Message]]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    grouped: dict[str, list[Message]] = {}
    for item in payload:
        grouped.setdefault(item["channel"], []).append(
            Message(
                text=item["text"],
                sender="fixture",
                timestamp=datetime.fromisoformat(item["timestamp"]),
                link=f"https://t.me/fixture/{item['message_id']}",
                channel_name=item["channel"],
                has_media=False,
                media_type="",
                message_id=item["message_id"],
                reply_to_id=item.get("reply_to_id"),
            )
        )
    return grouped


def _generator() -> ArticleGenerator:
    settings = Settings(
        schedule_time="09:00",
        timezone="Europe/Kyiv",
        lookback_hours=24,
        openai_model="gpt-5-nano",
        openai_temperature=0.7,
        output_language="Russian",
        target_user_id=123,
    )
    settings.article.generation_retries = 0
    config = Config(
        channels=[
            ChannelConfig(id="news", name="Городские новости", source_type="news"),
            ChannelConfig(id="utility", name="Коммунальное предприятие", source_type="official"),
            ChannelConfig(id="community", name="Бердянск", source_type="community"),
            ChannelConfig(id="classifieds", name="Объявления", source_type="classifieds"),
        ],
        settings=settings,
        telegram_api_id=123,
        telegram_api_hash="hash",
        telegram_bot_token="token",
        openai_api_key="key",
        log_level="INFO",
    )
    return ArticleGenerator(config, MagicMock())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rich_day_reaches_writer_as_cohesive_article_without_ad_or_rumor_dump():
    generator = _generator()
    messages = _load_messages()
    observed: dict[str, Any] = {}

    async def analyze(bundle, *, compact=False):
        assert compact is False
        observed["analysis_bundle_count"] = len(bundle.records)

        def refs_containing(needle: str) -> list[str]:
            return [
                ref
                for ref, record in bundle.records.items()
                if needle.lower() in record.message.text.lower()
            ]

        def element(text: str, refs: list[str], status: str) -> StoryElement:
            return StoryElement(text=text, source_refs=refs, status=status)

        cards = [
            StoryCard(
                id="SC001",
                topic="Водоснабжение",
                importance="high",
                summary="Коммунальное предприятие объявило плановое отключение воды.",
                hard_facts=[
                    element(
                        "15 августа воду отключат на улицах Центральной и Победы с 09:00 до 15:00.",
                        refs_containing("15 августа с 09:00"),
                        "established",
                    )
                ],
                community_observations=[
                    element(
                        "Жители сообщали о разной ситуации с водой в отдельных домах.",
                        refs_containing("воды нет") + refs_containing("Вода у нас появилась"),
                        "attributed",
                    )
                ],
            ),
            StoryCard(
                id="SC002",
                topic="Электроснабжение",
                importance="high",
                summary="Жители нескольких районов обсуждали перебои со светом.",
                community_observations=[
                    element(
                        "Жители Колонии, АКЗ и Морской писали о перебоях со светом.",
                        refs_containing("свет") + refs_containing("электричество"),
                        "attributed",
                    )
                ],
            ),
            StoryCard(
                id="SC003",
                topic="Связь",
                importance="medium",
                summary="Пользователи из разных районов жаловались на нестабильную связь.",
                community_observations=[
                    element(
                        "В сообщениях жителей упоминались перебои с мобильной связью и интернетом.",
                        refs_containing("связ")
                        + refs_containing("интернет")
                        + refs_containing("Onet"),
                        "attributed",
                    )
                ],
            ),
            StoryCard(
                id="SC004",
                topic="Происшествия",
                importance="high",
                summary="Полиция сообщила о ночных хлопках возле автовокзала.",
                hard_facts=[
                    element(
                        "Пострадавших по официальным данным нет, причина устанавливается.",
                        refs_containing("Полиция сообщила"),
                        "established",
                    )
                ],
            ),
            StoryCard(
                id="SC005",
                topic="Городская жизнь",
                importance="medium",
                summary="В городе объявили молодежную лабораторию и временное ограничение движения.",
                hard_facts=[
                    element(
                        "Городской молодежный центр объявил бесплатную двухдневную лабораторию.",
                        refs_containing("молодежный центр"),
                        "established",
                    ),
                    element(
                        "На участке улицы Победы временно ограничат движение.",
                        refs_containing("ограничено движение"),
                        "established",
                    ),
                ],
            ),
        ]
        assert all(card.all_source_refs() for card in cards)
        return EditorialAnalysis(cards=cards)

    async def write(analysis, bundle):
        observed["writer_bundle_count"] = len(bundle.records)
        observed["writer_bundle_text"] = bundle.prompt_text
        assert "Курс доллара" not in bundle.prompt_text
        assert "ОБНАЛИЧИВАНИЕ" not in bundle.prompt_text
        assert "Продам кондиционер" not in bundle.prompt_text
        assert "жители" in bundle.prompt_text.lower()
        assert "Коммунальное предприятие" in bundle.prompt_text
        return ArticleDraft(
            headline="Город за сутки: вода, свет и связь остаются главными темами",
            lead="Среди заметных тем суток — коммунальные ограничения, перебои со светом и нестабильная связь.",
            paragraphs=[
                "Коммунальное предприятие объявило плановое отключение воды на улицах Центральной и Победы 15 августа с 09:00 до 15:00.",
                "При этом сообщения жителей показывали разную ситуацию в отдельных домах: одни писали об отсутствии воды, другие — о восстановлении подачи.",
                "Перебои с электроснабжением обсуждали жители Колонии, АКЗ и улицы Морской. Сообщения описывали локальные отключения и кратковременное возвращение света.",
                "Пользователи также жаловались на нестабильную мобильную связь и интернет. В отдельных сообщениях упоминались разные районы и провайдеры.",
                "Городской отдел транспорта предупредил о временном ограничении движения на участке улицы Победы.",
                "Полиция сообщила о двух громких хлопках возле автовокзала. Данных о пострадавших нет, причина произошедшего устанавливается.",
                "Городской молодежный центр объявил бесплатную двухдневную лабораторию для студентов, регистрация на нее открыта до 25 августа.",
                "Часть сообщений жителей касалась очередей, зарядки телефонов и повседневных вопросов в районах города.",
            ],
            sections=[],
        )

    generator.analyzer.analyze = AsyncMock(side_effect=analyze)  # type: ignore[method-assign]
    generator.writer.write = AsyncMock(side_effect=write)  # type: ignore[method-assign]
    generator.fact_checker.check = AsyncMock(  # type: ignore[method-assign]
        return_value=FactCheckResult("PASS", False, [])
    )

    title, lead, body = await generator.generate_article(messages)

    assert observed["analysis_bundle_count"] >= 35
    assert observed["writer_bundle_count"] < observed["analysis_bundle_count"]
    assert title.startswith("Город за сутки")
    assert "Среди заметных тем" in lead
    assert body.count("\n\n") >= 9
    assert "Курс доллара" not in body
    assert "ОБНАЛИЧИВАНИЕ" not in body
    assert "беспилотник" not in body.lower()
    assert "авария на подстанции" not in body.lower()
    generator.writer.write.assert_awaited_once()
