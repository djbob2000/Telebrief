import pytest

from src.editorial_models import StoryCard
from src.publication.digest_narrative import (
    DigestNarrativeWriter,
    enforce_telegram_single_message_limit,
    parse_journalistic_markdown_to_draft,
)


class FakeProvider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls = []

    async def chat_completion(self, messages: list[dict], model: str | None = None) -> str:
        self.calls.append({"messages": messages, "model": model})
        if self.responses:
            return self.responses.pop(0)
        return ""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generate_journalistic_digest_single_pass_when_under_limit():
    short_text = (
        "Дайджест: Бердянск · 03.09.2026\n\n"
        "🔌 Электроснабжение\n"
        "• **Свет на Нагорной**: электроснабжение восстановлено к 14:00.\n\n"
        "💧 Водоснабжение\n"
        "• **Ремонт на Восточном**: подвоз воды организован у школы №3.\n"
    )
    provider = FakeProvider([short_text])
    writer = DigestNarrativeWriter(provider=provider)

    card1 = StoryCard(
        id="card_1",
        topic="Свет на Нагорной",
        summary="Восстановление электричества",
        importance="high",
        rubric_id="utilities",
    )
    card2 = StoryCard(
        id="card_2",
        topic="Ремонт на Восточном",
        summary="Подвоз воды",
        importance="medium",
        rubric_id="utilities",
    )

    result_text, draft = await writer.generate_journalistic_digest(
        city="Бердянск",
        date_str="03.09.2026",
        cards=[card1, card2],
        max_chars=3900,
    )

    assert len(provider.calls) == 1
    assert result_text == short_text.strip()
    assert len(draft.blocks) >= 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generate_journalistic_digest_two_pass_when_over_limit():
    long_item = (
        "• **Детали ремонта**: подробный отчет о работах на трансформаторной подстанции. " * 30
    )
    long_text = (
        "Дайджест: Бердянск · 03.09.2026\n\n"
        "🔌 Электроснабжение\n"
        f"{long_item}\n\n"
        "💧 Водоснабжение\n"
        f"{long_item}\n"
    )
    assert len(long_text) > 3900

    condensed_text = (
        "Дайджест: Бердянск · 03.09.2026\n\n"
        "🔌 Электроснабжение\n"
        "• **Подстанция**: ремонт завершен, напряжение подано.\n\n"
        "💧 Водоснабжение\n"
        "• **Восточный**: вода подается в штатном режиме.\n"
    )
    assert len(condensed_text) <= 3900

    provider = FakeProvider([long_text, condensed_text])
    writer = DigestNarrativeWriter(provider=provider)

    card1 = StoryCard(
        id="card_1",
        topic="Подстанция",
        summary="Ремонт завершен",
        importance="high",
        rubric_id="utilities",
    )

    result_text, draft = await writer.generate_journalistic_digest(
        city="Бердянск",
        date_str="03.09.2026",
        cards=[card1],
        max_chars=3900,
    )

    # Must have performed Pass 1 (generation) and Pass 2 (condensation)
    assert len(provider.calls) == 2
    assert result_text == condensed_text.strip()
    assert len(result_text) <= 3900
    assert len(draft.blocks) >= 1


@pytest.mark.unit
def test_enforce_telegram_single_message_limit():
    section1 = "🔌 Электроснабжение\n• Пункт 1\n• Пункт 2"
    section2 = "💧 Водоснабжение\n• Пункт 3\n• Пункт 4"
    section3 = "🏠 Городская среда\n• Пункт 5"

    full = f"{section1}\n\n{section2}\n\n{section3}"

    # If limit fits section 1 and 2 but not 3
    truncated = enforce_telegram_single_message_limit(
        full, max_chars=len(section1) + len(section2) + 10
    )
    assert section1 in truncated
    assert section2 in truncated
    assert section3 not in truncated
    assert len(truncated) <= len(section1) + len(section2) + 10


@pytest.mark.unit
def test_parse_journalistic_markdown_to_draft():
    markdown = (
        "Дайджест: Бердянск · 03.09.2026\n\n"
        "🔌 Электроснабжение\n"
        "• **Нагорная часть**: свет дали в 15:00.\n"
        "• **Коса**: отключения продолжаются до вечера.\n\n"
        "🛩️ Безопасность\n"
        "• **Сирена**: сигнал тревоги звучал около 11:30.\n"
    )

    card1 = StoryCard(
        id="s1",
        topic="Нагорная часть",
        summary="свет дали",
        importance="medium",
        rubric_id="utilities",
    )
    card2 = StoryCard(
        id="s2", topic="Коса", summary="отключения", importance="medium", rubric_id="utilities"
    )
    card3 = StoryCard(
        id="s3", topic="Сирена", summary="тревога", importance="high", rubric_id="safety"
    )

    draft = parse_journalistic_markdown_to_draft(markdown, cards=[card1, card2, card3])

    assert len(draft.blocks) >= 2
    total_items = sum(len(b.items) for b in draft.blocks)
    assert total_items == 3
    # Check that story IDs were mapped
    found_sids = [sid for b in draft.blocks for it in b.items for sid in it.covered_story_ids]
    assert "s1" in found_sids or "s2" in found_sids or "s3" in found_sids


@pytest.mark.unit
def test_parse_journalistic_markdown_custom_branch_format():
    custom_text = (
        "Дайджест · 04 сентября 2026\n\n"
        "Коммунальная обстановка\n\n"
        "⚡ В городе продолжаются масштабные перебои с центральным электроснабжением: в ряде районов воду подают с помощью генераторов по графику с 17:00 до 21:00, а днём на отдельных участках фиксировались кратковременные скачки напряжения.\n\n"
        "📄 Горожане обратили внимание на получение квитанций за август с объединёнными счетами за коммунальные услуги, включая корректировки и начисления за свет и отопление.\n\n"
        "Безопасность и чрезвычайные ситуации\n\n"
        "💥 Сообщения о взрывах в городе и районе: Поздним вечером 3 сентября (в промежутке между 21:00 и 22:20) жители Бердянска и населённых пунктов района сообщили о серии громких звуков взрывов, шуме беспилотников и вспышках в небе.\n\n"
        "Социальная помощь\n\n"
        "💳 Зачисление детских выплат: Начиная с 3 сентября жители города начали получать регулярные начисления единого ежемесячного пособия и детских выплат на карты банков.\n"
    )
    draft = parse_journalistic_markdown_to_draft(custom_text, cards=[])
    assert len(draft.blocks) == 3
    assert draft.blocks[0].block_id.startswith("block:infrastructure:")
    assert len(draft.blocks[0].items) == 2
    assert draft.blocks[1].block_id.startswith("block:safety:")
    assert len(draft.blocks[1].items) == 1
    assert draft.blocks[2].block_id.startswith("block:social:") or draft.blocks[
        2
    ].block_id.startswith("block:education:")
    assert len(draft.blocks[2].items) == 1
