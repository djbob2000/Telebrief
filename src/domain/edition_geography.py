"""Domain models and resolution for edition-specific geographic boundaries."""

from __future__ import annotations

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
    "«Гора» / «Нагорная часть города» — крупный исторический и административный район города на возвышенности, объединяющий жилые массивы АЗМОЛ, Стекловолокно, АКЗ, РТС, Военный городок и 8 Марта. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО называть его «микрорайон Гора». В тексте использовать: «в нагорной части города» или «на Горе».",
    "«Осипенко» — отдельное село Бердянского района. Это именно село (НЕ посёлок и НЕ микрорайон города). В тексте писать естественно: «в селе Осипенко» (не добавлять лишних пояснений о расстоянии/километраже до города; категорически запрещено писать «в посёлке Осипенко» или «в микрорайоне Осипенко»).",
    "«Миранда» («Миранда-медиа») — региональный телеком- и интернет-провайдер. НЕ является горой, возвышенностью или частью топонима «гора Миранда». Фраза «на Горе Миранда» означает «в нагорной части города (на Горе) интернет-провайдер Миранда».",
    "«50 лет» / «50 лет СССР» — обиходное название улицы 50 лет СССР (официально переименованной в ул. Нагорную), проходящей в Нагорной части города параллельно Мелитопольскому шоссе («в районе улицы 50 лет СССР / Нагорной»), а не абстрактный «район 50-летия».",
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
