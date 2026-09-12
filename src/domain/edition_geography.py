"""Domain models and resolution for edition-specific geographic boundaries."""

import re
from dataclasses import dataclass, field
from typing import Any

# Built-in geographic definitions for primary editions
_BERDYANSK_TARGET_LOCATIONS = (
    "Бердянск",
    "Нагорная часть (Гора)",
    "Гора",
    "Нагорная часть",
    "Центр",
    "Лиски",
    "Слободка",
    "АКЗ",
    "РТС",
    "Азмол",
    "Стекловолокно",
    "Колония",
    "Макорты",
    "8 Марта",
    "Военный городок",
    "Бердянская коса",
    "Ближняя коса",
    "Средняя коса",
    "Дальняя коса",
    "Бердянский залив",
    "Черемушки",
    "Аэропорт",
    "Шевченко",
)

_BERDYANSK_DISTRICT_LOCATIONS = (
    "Бердянский район",
    "Осипенко (село Бердянского района)",
    "Азовское (село)",
    "Луначарское (село)",
    "Нововасильевка (село)",
    "Дмитровка (село)",
    "Андреевка",
    "Берестовое",
    "Черниговка",
    "Приморск",
    "Приморский район",
)

_BERDYANSK_TOPONYM_RULES = (
    "«Гора» / «Нагорная часть города» — крупный исторический и административный район города на возвышенности, объединяющий жилые массивы АЗМОЛ, Стекловолокно, АКЗ, РТС, Военный городок и 8 Марта. В тексте использовать: «в нагорной части города» или «на Горе» (не «микрорайон Гора»).",
    "«Осипенко» — отдельное село Бердянского района. Это именно село (не посёлок и не микрорайон города). В тексте писать естественно: «в селе Осипенко».",
    "«Миранда» («Миранда-медиа») — региональный телеком- и интернет-провайдер. Не является возвышенностью или частью топонима «гора Миранда». Фраза «на Горе Миранда» означает «в нагорной части города (на Горе) интернет-провайдер Миранда».",
    "«50 лет» / «50 лет СССР» — обиходное название улицы 50 лет СССР (официально переименованной в ул. Нагорную), проходящей в Нагорной части города («в районе улицы 50 лет СССР / Нагорной»), а не абстрактный «район 50-летия».",
    "«Юпитер» — местный интернет-провайдер в Бердянске (не район и не микрорайон города). Фразы вроде «в Юпитере нет оптоволокна» означают «у провайдера «Юпитер» нет оптоволокна» или «в сети «Юпитер»».",
    "Памятник «Самолёт» — известный памятник и городской ориентир в Бердянске на пересечении улицы Довганюка (Орджоникидзе) и Восточного (Пролетарского) проспекта. В селе Осипенко Бердянского района установлен ещё один одноимённый памятник самолёту (различать город и село по контексту и улицам). В русскоязычном тексте использовать: «у памятника Самолёту», «возле памятника Самолёту» или «возле Самолёта» (не «у «Літака»»). Слово «ліхтарі» переводить на русский как «фонари».",
    "«АКЗ» и «АЗМОЛ» — это два РАЗНЫХ, самостоятельных жилых массива (микрорайона) города. АКЗ не расшифровывается как Азмол и не имеет к нему отношения (АКЗ назван по Азовкабель/автомобильному заводу, АЗМОЛ — по заводу смазок). КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО называть АКЗ «Азмоловским комплексом» или объединять их в одну сущность!",
    "«Лиски» — прибрежный микрорайон в нижней части города Бердянска. С предлогами употребляется строго «на»: «на Лисках», «на Лиски» (КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать «в Лисках» или «в Лиски»!).",
    "«Зеркальный» («Дзеркальний») — крупный супермаркет и известный ориентир в Бердянске (в советские времена — Универмаг; в этом же здании спортклуб ARENA / «АРЕНА»; позже супермаркет был переименован в «Илэар»). «Зеркальный» — это сам супермаркет, а не ориентир рядом (НЕ кинотеатр!). Товар вывозят именно из самого здания супермаркета. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать «магазин у «Зеркального»» или «магазин возле «Зеркального»»! В тексте писать: «вывоз товара из «Зеркального»», «из супермаркета «Зеркальный»» или «супермаркет «Зеркальный» вывозит товар» (допустимо уточнение: «бывший супермаркет «Зеркальный», переименованный в «Илэар»»).",
)


_COMMON_OUT_OF_SCOPE_LOCATIONS = (
    "Киев",
    "Харьков",
    "Одесса",
    "Львов",
    "Днепр",
    "Москва",
    "Санкт-Петербург",
    "Краснодар",
    "Севастополь",
    "Симферополь",
    "Мариуполь",
    "Мелитополь",
    "Энергодар",
    "Пологи",
    "Токмак",
    "Васильевка",
)


@dataclass(frozen=True)
class EditionGeographyContext:
    """Geographic scope configuration and reference anchors for an edition."""

    edition_slug: str
    edition_name: str
    target_locations: tuple[str, ...]
    district_locations: tuple[str, ...] = ()
    region_name: str = "Запорожская область"
    neighboring_regions: tuple[str, ...] = (
        "Донецкая область",
        "Херсонская область",
        "Ростовская область",
    )
    out_of_scope_locations: tuple[str, ...] = _COMMON_OUT_OF_SCOPE_LOCATIONS
    rules: dict[str, Any] = field(default_factory=dict)
    toponym_rules: tuple[str, ...] = ()

    @property
    def slug(self) -> str:
        return self.edition_slug

    def to_prompt_section(self) -> str:
        """Format geographic reference rules for inclusion in LLM triage and analysis prompts."""
        target_str = ", ".join(self.target_locations[:25])
        district_str = ", ".join(self.district_locations[:15]) if self.district_locations else "нет"
        out_str = ", ".join(self.out_of_scope_locations[:10])

        lines = [
            f"=== ГЕОГРАФИЧЕСКИЙ КОНТЕКСТ ИЗДАНИЯ ({self.edition_name}) ===",
            f"Целевой город и районы (LOCAL): {target_str}",
            f"Прилегающий район (DIRECT_IMPACT при прямом влиянии на город): {district_str}",
            f"Область / регион: {self.region_name}",
            f"За пределами охвата (OUT_OF_SCOPE, если нет прямого влияния на {self.edition_name}): {out_str}",
            "ПРАВИЛО ГЕОГРАФИЧЕСКОГО ОХВАТА:",
            f" - LOCAL: события происходят непосредственно в г. {self.edition_name} или его районах.",
            f" - DIRECT_IMPACT: региональные события ({self.region_name}), непосредственно влияющие на жизнедеятельность, снабжение или безопасность г. {self.edition_name}.",
            f" - OUT_OF_SCOPE: события других городов ({out_str}), программы помощи и мероприятия для переселенцев/ВПО за пределами города, деятельность релоцированных администраций в других регионах, федеральная/мировая политика без прямой связи с {self.edition_name} -> DROP.",
        ]
        if self.toponym_rules:
            lines.append("ВАЖНЫЕ МЕСТНЫЕ ТОПОНИМЫ И РАЗЛИЧЕНИЕ СУЩНОСТЕЙ:")
            for r in self.toponym_rules:
                lines.append(f" - {r}")

        return "\n".join(lines) + "\n"


def resolve_edition_geography(
    edition_slug: str,
    edition_name: str = "",
) -> EditionGeographyContext:
    """Resolve standard or generic geographic context for an edition slug."""
    slug = (edition_slug or "").strip().lower()
    name = edition_name.strip() if edition_name else slug.capitalize()

    if slug in ("berdyansk", "бердянск"):
        return EditionGeographyContext(
            edition_slug="berdyansk",
            edition_name=name or "Бердянск",
            target_locations=_BERDYANSK_TARGET_LOCATIONS,
            district_locations=_BERDYANSK_DISTRICT_LOCATIONS,
            region_name="Запорожская область",
            neighboring_regions=("Донецкая область", "Херсонская область", "Ростовская область"),
            out_of_scope_locations=_COMMON_OUT_OF_SCOPE_LOCATIONS,
            toponym_rules=_BERDYANSK_TOPONYM_RULES,
        )

    # Generic fallback
    return EditionGeographyContext(
        edition_slug=slug,
        edition_name=name,
        target_locations=(name, f"г. {name}", f"город {name}"),
        district_locations=(f"{name}ский район",),
        region_name="Регион",
        out_of_scope_locations=_COMMON_OUT_OF_SCOPE_LOCATIONS,
    )


def normalize_edition_toponyms(text: str, edition_slug: str = "berdyansk") -> str:
    """Normalize well-known edition toponym and entity misattributions in Russian-language text."""
    if not text:
        return ""
    slug = (edition_slug or "").strip().lower()
    if slug not in ("berdyansk", "бердянск"):
        return text

    res = text

    # 1. Юпитер (местный интернет-провайдер, не район города)
    def _sub_jupiter_in(m: re.Match[str]) -> str:
        is_cap = m.group(0)[0].isupper()
        return "У провайдера «Юпитер»" if is_cap else "у провайдера «Юпитер»"

    res = re.sub(
        r"\b[Вв]\s+(?:бердянском\s+)?районе\s+[«\"“]?Юпитер[»\"”]?",
        _sub_jupiter_in,
        res,
    )
    res = re.sub(
        r"\b[Вв]\s+[«\"“]?Юпитере[»\"”]?\s+пока\s+(?:ещ[её]\s+)?(?:его\s+)?(?:нет|отсутствует)\b",
        lambda m: (
            "У провайдера «Юпитер» пока отсутствует"
            if m.group(0)[0].isupper()
            else "у провайдера «Юпитер» пока отсутствует"
        ),
        res,
    )
    res = re.sub(
        r"\b(?:[Бб]ердянск(?:ий|ом)\s+)?район(?:е)?\s+[«\"“]?Юпитер[»\"”]?",
        lambda m: "Провайдер «Юпитер»" if m.group(0)[0].isupper() else "провайдер «Юпитер»",
        res,
    )
    res = re.sub(
        r"\b[«\"“]?Юпитер[»\"”]?\s+(?:район|микрорайон)\b",
        "провайдер «Юпитер»",
        res,
        flags=re.IGNORECASE,
    )

    # 2. Памятник Самолёт / Літак
    def _sub_litak(m: re.Match[str]) -> str:
        prep = m.group(1)
        is_cap = prep[0].isupper()
        return "У памятника Самолёту" if is_cap else "у памятника Самолёту"

    res = re.sub(
        r"\b(У|у|Возле|возле|Около|около|Біля|біля)\s+[«\"“]?[Лл][іiи]так[аa]?[»\"”]?",
        _sub_litak,
        res,
    )
    res = re.sub(
        r"\b(?:біля|возле|у)\s+пам[\'’]ятник(?:а)?\s+[«\"“]?[Лл][іiи]так[»\"”]?",
        "возле памятника Самолёту",
        res,
        flags=re.IGNORECASE,
    )
    res = re.sub(
        r"\bпам[\'’]ятник(?:а)?\s+[«\"“]?[Лл][іiи]так[»\"”]?",
        "памятника Самолёту",
        res,
        flags=re.IGNORECASE,
    )
    res = re.sub(
        r"\bпамятник(?:а)?\s+[«\"“]?[Лл][іiи]так[»\"”]?",
        "памятника Самолёту",
        res,
        flags=re.IGNORECASE,
    )

    # 3. Лихтари / ліхтарі -> фонари
    res = re.sub(
        r"\b[Лл]ихтар(?:и|ей|ям|ями|ях|я|ь|ем|ём)?\b",
        lambda m: (
            ("Фонар" if m.group(0)[0].isupper() else "фонар")
            + {
                "и": "и",
                "ей": "ей",
                "ям": "ям",
                "ями": "ями",
                "ях": "ях",
                "я": "я",
                "ь": "ь",
                "ем": "ём",
                "ём": "ём",
            }.get(m.group(0)[6:].lower(), "и")
        ),
        res,
    )
    res = re.sub(
        r"\b[Лл]іхтар(?:і|ів|ям|ями|ях|я|ь|ем|ем)?\b",
        lambda m: (
            ("Фонар" if m.group(0)[0].isupper() else "фонар")
            + {
                "і": "и",
                "ів": "ей",
                "ям": "ям",
                "ями": "ями",
                "ях": "ях",
                "я": "я",
                "ь": "ь",
                "ем": "ём",
            }.get(m.group(0)[6:].lower(), "и")
        ),
        res,
    )

    # 4. Осипенко (отдельное село)
    res = re.sub(
        r"\bв\s+(?:микрорайоне|пос[её]лке)\s+Осипенко\b",
        "в селе Осипенко",
        res,
        flags=re.IGNORECASE,
    )
    res = re.sub(
        r"\b(?:микрорайон(?:е)?|пос[её]лок(?:е)?)\s+Осипенко\b",
        "село Осипенко",
        res,
        flags=re.IGNORECASE,
    )

    # 5. Гора (нагорная часть города)
    res = re.sub(
        r"\bв\s+микрорайоне\s+Гора\b",
        "в нагорной части города (на Горе)",
        res,
        flags=re.IGNORECASE,
    )
    res = re.sub(
        r"\bмикрорайон(?:е)?\s+Гора\b",
        "Нагорная часть (Гора)",
        res,
        flags=re.IGNORECASE,
    )

    # 6. Миранда (интернет-провайдер)
    res = re.sub(
        r"\bна\s+горе\s+Миранда\b",
        "на Горе у провайдера «Миранда»",
        res,
        flags=re.IGNORECASE,
    )
    res = re.sub(
        r"\bгора\s+Миранда\b",
        "провайдер «Миранда» на Горе",
        res,
        flags=re.IGNORECASE,
    )

    # 7. 50 лет СССР
    res = re.sub(
        r"\bв\s+районе\s+50[- ]?летия\b",
        "в районе улицы 50 лет СССР (Нагорной)",
        res,
        flags=re.IGNORECASE,
    )

    # 8. АКЗ / Азмоловский комплекс (АКЗ ошибочно называют Азмоловским комплексом)
    res = re.sub(
        r"\b[Аа]змоловск(?:ий|ого|ому|им|ом)\s+комплекс(?:а|у|ом|е)?\b",
        "АКЗ",
        res,
    )

    # 9. Лиски (микрорайон в нижней части города: строго «на Лисках», «на Лиски»)
    res = re.sub(
        r"\bВ\s+Лисках\b",
        "На Лисках",
        res,
    )
    res = re.sub(
        r"\bв\s+Лисках\b",
        "на Лисках",
        res,
    )
    res = re.sub(
        r"\bВ\s+Лиски\b",
        "На Лиски",
        res,
    )
    res = re.sub(
        r"\bв\s+Лиски\b",
        "на Лиски",
        res,
    )

    # 10. «Зеркальный» (крупный супермаркет, не кинотеатр и не «магазин у Зеркального»)
    # 10.1 Кинотеатр -> бывший супермаркет
    res = re.sub(
        r"(?:\b(?:возле|рядом\s+с|у)\s+)?кинотеатр[а-я]*\s+[«\"]?(?:Д)?зеркальн[а-я]*[»\"]?",
        "из бывшего супермаркета «Зеркальный»",
        res,
        flags=re.IGNORECASE,
    )
    # 10.2 Замена «В Бердянске магазин возле «Дзеркального»/«Зеркального» вывозит товар»
    res = re.sub(
        r"\bВ\s+Бердянске\s+магазин\s+(?:у|возле|около|рядом\s+с)\s+[«\"]?(?:З|Дз)еркальн[а-я]*[»\"]?\s+вывозит\s+товар\b",
        "В Бердянске из супермаркета «Зеркальный» вывозят товар",
        res,
        flags=re.IGNORECASE,
    )
    # 10.3 Замена заголовков вида «Магазин у/возле «Зеркального» вывозит товар»
    res = re.sub(
        r"\b(?:Магазин|магазин)\s+(?:у|возле|около|рядом\s+с)\s+[«\"]?(?:З|Дз)еркальн[а-я]*[»\"]?\s+вывозит\s+товар\b",
        "Вывоз товара из «Зеркального»",
        res,
    )
    # 10.4 Замена «Возле бывшего супермаркета «Зеркальный» ... магазин вывозит товар / вывозят товар»
    res = re.sub(
        r"\b(?:[Вв]озле|[Уу]|[Рр]ядом\s+с)\s+бывш(?:его|им)\s+супермаркет(?:а|ом)\s+[«\"]?(?:З|Дз)еркальн[а-я]*[»\"]?(?:\s+в\s+Бердянске)?(?:\s+местные\s+жители\s+заметили,\s+что)?(?:\s+из\s+магазина|\s+магазин)?\s+вывоз(?:ят|ит)\s+товар\b",
        "В Бердянске из бывшего супермаркета «Зеркальный» вывозят товар",
        res,
        flags=re.IGNORECASE,
    )
    # 10.5 Одиночные «магазин у Зеркального» -> «супермаркет «Зеркальный»»
    res = re.sub(
        r"\b(?:Магазин|магазин)\s+(?:у|возле|около|рядом\s+с)\s+[«\"]?(?:З|Дз)еркальн[а-я]*[»\"]?\b",
        "супермаркет «Зеркальный»",
        res,
    )
    # 10.6 Украиноязычное написание «Дзеркальний»
    res = re.sub(
        r"[«\"]Дзеркальн(?:ий|ый)[»\"]",
        "«Зеркальный»",
        res,
    )

    return res


def normalize_edition_operational_location_and_entity(
    loc: str, entity: str = "", edition_slug: str = "berdyansk"
) -> tuple[str, str]:
    """Normalize colloquial anomalies in location/entity for a specific edition."""
    loc_clean = loc.strip() if loc else ""
    ent_clean = entity.strip() if entity else ""

    slug = (edition_slug or "").strip().lower()
    if slug not in ("berdyansk", "бердянск"):
        return loc_clean, ent_clean

    # 1. "гора Миранда" -> loc="Нагорная часть (Гора)", entity="Миранда"
    if re.search(r"\bгора\s+миранда\b|\bмиранда\s*\(гора\)", loc_clean, re.IGNORECASE):
        loc_clean = "Нагорная часть (Гора)"
        if not ent_clean:
            ent_clean = "Миранда"
    elif "миранда" in loc_clean.lower() and not ent_clean:
        ent_clean = "Миранда"
        loc_clean = re.sub(r"\bмиранда\b", "", loc_clean, flags=re.IGNORECASE).strip(" ,()")

    # 2. "микрорайон Гора" -> "Нагорная часть (Гора)"
    if re.search(r"\bмикрорайон\s+гора\b|\bмкр\.?\s*гора\b", loc_clean, re.IGNORECASE):
        loc_clean = re.sub(
            r"\b(?:микрорайон|мкр\.?)\s+гора\b",
            "Нагорная часть (Гора)",
            loc_clean,
            flags=re.IGNORECASE,
        )

    # 3. "район 50-летия" / "на 50-летие" / "50 лет" -> "ул. 50 лет СССР (Нагорная)"
    if re.search(
        r"\b(?:район\s+50[- ]?летия|на\s+50[- ]?лет(?:ие)?|50[- ]?летия)\b",
        loc_clean,
        re.IGNORECASE,
    ):
        loc_clean = re.sub(
            r"\b(?:район\s+50[- ]?летия|на\s+50[- ]?лет(?:ие)?|50[- ]?летия)\b",
            "ул. 50 лет СССР (Нагорная)",
            loc_clean,
            flags=re.IGNORECASE,
        )

    # 4. "микрорайон/посёлок Осипенко" -> "село Осипенко"
    if re.search(
        r"\b(?:микрорайон|мкр\.?|пос[её]лок|пос\.?)\s+осипенко\b", loc_clean, re.IGNORECASE
    ):
        loc_clean = re.sub(
            r"\b(?:микрорайон|мкр\.?|пос[её]лок|пос\.?)\s+осипенко\b",
            "село Осипенко",
            loc_clean,
            flags=re.IGNORECASE,
        )

    # 5. "Юпитер" / "район Юпитер" -> ent="Юпитер", loc_clean stripped of Юпитер
    if re.search(r"\b[«\"“]?юпитер[»\"”]?\b", loc_clean, re.IGNORECASE):
        if not ent_clean:
            ent_clean = "Юпитер"
        loc_clean = re.sub(
            r"\b(?:в\s+)?(?:район(?:е|а)?|микрорайон(?:е|а)?|мкр\.?)?\s*[«\"“]?юпитер[»\"”]?\s*(?:район(?:е|а)?|микрорайон(?:е|а)?|мкр\.?)?\b",
            "",
            loc_clean,
            flags=re.IGNORECASE,
        ).strip(" ,()")

    # 6. "Самолёт" / "Літак"
    if re.search(r"\b(?:[Лл][іiи]так(?:а)?|[Сс]амол[её]т(?:а)?)\b", loc_clean, re.IGNORECASE):
        if re.search(r"\bосипенко\b", loc_clean, re.IGNORECASE):
            loc_clean = "село Осипенко (памятник Самолёту)"
        else:
            loc_clean = "памятник Самолёту (пересечение ул. Довганюка и Восточного пр.)"

    return loc_clean, ent_clean
