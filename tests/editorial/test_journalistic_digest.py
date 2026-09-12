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
    expected_body = (
        "🔌 Электроснабжение\n"
        "• **Свет на Нагорной**: электроснабжение восстановлено к 14:00.\n\n"
        "💧 Водоснабжение\n"
        "• **Ремонт на Восточном**: подвоз воды организован у школы №3."
    )
    assert result_text == expected_body
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
    expected_condensed_body = (
        "🔌 Электроснабжение\n"
        "• **Подстанция**: ремонт завершен, напряжение подано.\n\n"
        "💧 Водоснабжение\n"
        "• **Восточный**: вода подается в штатном режиме."
    )
    assert result_text == expected_condensed_body
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


@pytest.mark.unit
def test_parse_journalistic_markdown_bullet_list_sept_05_sample():
    sample_text = (
        "Дайджест · 05 сентября 2026\n\n"
        "В фокусе внимания\n\n"
        "💨 Массовые сообщения о запахе газа в городе: Жители большинства районов Бердянска "
        "(АКЗ, РТС, Слободка, Центр, Колония, 8 Марта) сообщили о резком запахе газа на улицах и в жилых помещениях. "
        "По информации специалистов, ситуация вызвана проведением технических работ на распределительных станциях "
        "и повышенной концентрацией специального пахучего маркера (одоранта) в газовой магистрали; утечек на сетях не зафиксировано. "
        "Горожане также отмечали временные трудности с дозвоном по номерам экстренной газовой службы.\n\n"
        "Коммунальная обстановка\n\n"
        "⚡️ Обновление квитанций за коммунальные услуги: На фоне сохраняющихся проблем с электроснабжением горожане обратили внимание "
        "на включение строки оплаты за услуги теплосети в единые платежные документы за электричество. "
        "Коммунальные службы уточнили, что при проведении онлайн-платежей счета можно разделять, "
        "оплачивая только фактически потребленную электроэнергию.\n\n"
        "Безопасность и чрезвычайные ситуации\n\n"
        "🛡 В ночные часы над городом и со стороны морского побережья фиксировались пролеты БПЛА, "
        "а жители сообщали о серии громких звуков стрельбы в небе и работы ПВО.\n\n"
        "💥 Поступают сообщения от жителей о звуках стрельбы и взрыве в черте Бердянска; "
        "информация о причинах и последствиях происшествия уточняется.\n\n"
        "💥 Вечером в Бердянске были слышны громкие звуки и взрывы, при этом информация о возможных последствиях, "
        "причинах инцидента или работе ПВО носит предварительный характер и уточняется.\n\n"
        "Связь и интернет\n\n"
        "🌐 Ремонт магистральных интернет-сетей: Провайдер «Юпитер» приступил к утренним восстановительным работам на магистральной линии связи, "
        "в связи с чем у абонентов в ряде районов города временно наблюдаются перебои с доступом к сети.\n\n"
        "Социальная помощь\n\n"
        "🏢 Анонс создания центров бытовой помощи: В регионе заявлено о планах по открытию пунктов поддержки населения. "
        "В центрах планируется организовать зоны для стирки и глажки одежды, душевые комнаты, раздачу питьевой воды, "
        "а также точки подзарядки гаджетов и доступ к сети Wi-Fi.\n\n"
        "Другое\n\n"
        "🚫 С 1 сентября вступил в силу полный запрет на розничную торговлю алкогольной продукцией; "
        "за нарушение ограничений для предпринимателей предусмотрены штрафы и меры административной ответственности.\n\n"
        "👨‍👩‍👧 Служба по делам детей исполкома Бердянского городского совета сообщила о начале приема заявок на национальную премию "
        "«Родина для кожної дитини — Better Care Awards», направленную на поддержку практик семейного ухода и воспитания детей.\n\n"
        "📚 В учебных заведениях Бердянской городской общины состоялись специализированные тематические уроки "
        "«Мова гідності» и «Пам’яті», посвященные украинской государственности и чествованию защитников.\n"
    )

    draft = parse_journalistic_markdown_to_draft(sample_text, cards=[])
    assert len(draft.blocks) == 6

    # Block 0: В фокусе внимания
    assert draft.blocks[0].block_id.startswith("block:focus:")
    assert len(draft.blocks[0].items) == 1
    assert "запахе газа" in draft.blocks[0].items[0].headline.lower()

    # Block 1: Коммунальная обстановка
    assert draft.blocks[1].block_id.startswith("block:infrastructure:")
    assert len(draft.blocks[1].items) == 1
    assert "квитанций" in draft.blocks[1].items[0].headline.lower()

    # Block 2: Безопасность и чрезвычайные ситуации
    assert draft.blocks[2].block_id.startswith("block:safety:")
    assert len(draft.blocks[2].items) == 3

    # Block 3: Связь и интернет
    assert draft.blocks[3].block_id.startswith("block:communications:")
    assert len(draft.blocks[3].items) == 1
    assert "интернет" in draft.blocks[3].items[0].headline.lower()

    # Block 4: Социальная помощь
    assert draft.blocks[4].block_id.startswith("block:social:")
    assert len(draft.blocks[4].items) == 1

    # Block 5: Другое
    assert draft.blocks[5].block_id.startswith("block:general:")
    assert len(draft.blocks[5].items) == 3


@pytest.mark.unit
def test_parse_journalistic_markdown_with_custom_rubrics():
    sample_text = (
        "Дайджест · 06 сентября 2026\n\n"
        "Экология и море\n\n"
        "🌊 На центральном пляже завершили очистку прибрежной полосы после шторма.\n\n"
        "Культура и искусство\n\n"
        "🎨 В городском музее открылась выставка работ местных художников.\n"
    )

    from src.config.schemas.publication import DigestGroupConfig

    custom_groups = [
        DigestGroupConfig(name="Экология и море", description="Экологические новости"),
        DigestGroupConfig(name="Культура и искусство", description="Культурная жизнь"),
    ]

    draft = parse_journalistic_markdown_to_draft(
        sample_text, cards=[], custom_rubrics=custom_groups
    )
    assert len(draft.blocks) == 2
    assert "экология" in draft.blocks[0].block_id or "custom" in draft.blocks[0].block_id
    assert len(draft.blocks[0].items) == 1
    assert "Культура" in draft.blocks[1].block_id or "культура" in draft.blocks[1].block_id
    assert len(draft.blocks[1].items) == 1


@pytest.mark.unit
def test_parse_journalistic_markdown_sept_11_sample():
    sample_text = (
        "Дайджест · 11 сентября 2026\n\n"
        "Коммунальная обстановка\n\n"
        "💧 **Проблемы с водоснабжением в многоэтажках:** Жители Бердянска сообщают, что в многоэтажных домах вода не поднимается выше 6-7 этажа, из-за чего жители верхних этажей испытывают серьезные неудобства.\n\n"
        "🔌 **Нестабильная подача электроэнергии в городе:** В Бердянске фиксируются перебои с электричеством. Жители сообщают, что в некоторых районах (в частности, на АЗМОЛ) света нет с ночи, при этом в других частях города (Лиски, Лиепайская) электричество присутствует, однако позже поступила информация о его отключении и там.\n\n"
        "⚡️ **Низкое напряжение в сети:** Жители сообщают, что в электросети города фиксируется крайне низкое напряжение — порядка 130 Вольт, которое недостаточно для работы электроприборов.\n\n"
        "Безопасность и чрезвычайные ситуации\n\n"
        "🔥 **Пожар на подстанции в Бердянске:** По сообщениям жителей, в городе горит электроподстанция. Предположительная причина — короткое замыкание, при этом очевидцы отмечают, что хлопков и взрывов перед возгоранием слышно не было.\n\n"
        "🔹 **Синяя вспышка в небе над городом:** Житель Бердянска сообщил о наблюдении яркой синей вспышки в одном из районов города. По словам очевидца, взрывов или других звуков за вспышкой не последовало.\n\n"
        "Связь и интернет\n\n"
        "🌐 **Отключение интернета от провайдера «Юпитер»:** Согласно сообщению провайдера «Юпитер», с 00:00 до 12:00 ожидается отсутствие доступа в интернет у абонентов данной сети.\n\n"
        "Городская среда и бизнес\n\n"
        "📦 **Магазин у «Зеркального» вывозит товар:** Возле бывшего супермаркета «Дзеркальний» в Бердянске местные жители заметили, что из магазина вывозят товар. Это может свидетельствовать о скором закрытии торговой точки.\n\n"
        "Другое\n\n"
        "⚽️ **Набор детей на футбол в спортивной школе:** Спортивная школа им. Назарова в Бердянске продолжает набор мальчиков 2017 и 2018 годов рождения на занятия футболом.\n"
    )

    draft = parse_journalistic_markdown_to_draft(sample_text, cards=[])
    assert len(draft.blocks) == 5

    # Block 0: Коммунальная обстановка (infrastructure)
    assert draft.blocks[0].block_id.startswith("block:infrastructure:")
    assert len(draft.blocks[0].items) == 3
    assert "водоснабжением" in draft.blocks[0].items[0].headline.lower()
    assert "электроэнергии" in draft.blocks[0].items[1].headline.lower()
    assert "напряжение" in draft.blocks[0].items[2].headline.lower()

    # Block 1: Безопасность и чрезвычайные ситуации (safety)
    assert draft.blocks[1].block_id.startswith("block:safety:")
    assert len(draft.blocks[1].items) == 2
    assert "пожар" in draft.blocks[1].items[0].headline.lower()
    assert "вспышка" in draft.blocks[1].items[1].headline.lower()

    # Block 2: Связь и интернет (communications)
    assert draft.blocks[2].block_id.startswith("block:communications:")
    assert len(draft.blocks[2].items) == 1
    assert "юпитер" in draft.blocks[2].items[0].headline.lower()

    # Block 3: Городская среда и бизнес (urban_life)
    assert draft.blocks[3].block_id.startswith("block:urban_life:")
    assert len(draft.blocks[3].items) == 1
    assert "зеркального" in draft.blocks[3].items[0].headline.lower()

    # Block 4: Другое (general)
    assert draft.blocks[4].block_id.startswith("block:general:")
    assert len(draft.blocks[4].items) == 1
    assert "футбол" in draft.blocks[4].items[0].headline.lower()
