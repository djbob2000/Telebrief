"""Domain models and resolution for edition-specific geographic boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Built-in geographic definitions for primary editions
_BERDYANSK_TARGET_LOCATIONS = (
    "Бердянск",
    "Бердянский залив",
    "Бердянская коса",
    "Ближняя коса",
    "Средняя коса",
    "Дальняя коса",
    "АКЗ",
    "РТС",
    "Азмол",
    "Колония",
    "Слободка",
    "Лиски",
    "Нагорная часть",
    "Центр",
    "Военный городок",
    "Стекловолокно",
    "8 Марта",
    "Черемушки",
    "Аэропорт",
    "Макорты",
    "Шевченко",
)

_BERDYANSK_DISTRICT_LOCATIONS = (
    "Бердянский район",
    "Азовское",
    "Луначарское",
    "Нововасильевка",
    "Осипенко",
    "Дмитровка",
    "Андреевка",
    "Берестовое",
    "Черниговка",
    "Приморск",
    "Приморский район",
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

    def to_prompt_section(self) -> str:
        """Format geographic reference rules for inclusion in LLM triage and analysis prompts."""
        target_str = ", ".join(self.target_locations[:12])
        district_str = ", ".join(self.district_locations[:8]) if self.district_locations else "нет"
        out_str = ", ".join(self.out_of_scope_locations[:10])

        return (
            f"=== ГЕОГРАФИЧЕСКИЙ КОНТЕКСТ ИЗДАНИЯ ({self.edition_name}) ===\n"
            f"Целевой город и районы (LOCAL): {target_str}\n"
            f"Прилегающий район (DIRECT_IMPACT при прямом влиянии на город): {district_str}\n"
            f"Область / регион: {self.region_name}\n"
            f"За пределами охвата (OUT_OF_SCOPE, если нет прямого влияния на {self.edition_name}): {out_str}\n"
            f"ПРАВИЛО ГЕОГРАФИЧЕСКОГО ОХВАТА:\n"
            f" - LOCAL: события происходят непосредственно в г. {self.edition_name} или его районах.\n"
            f" - DIRECT_IMPACT: региональные события ({self.region_name}), непосредственно влияющие на жизнедеятельность, снабжение или безопасность г. {self.edition_name}.\n"
            f" - OUT_OF_SCOPE: события других городов ({out_str}), федеральная/мировая политика без прямой связи с {self.edition_name} -> DROP.\n"
        )


def resolve_edition_geography(
    edition_slug: str,
    edition_name: str = "",
) -> EditionGeographyContext:
    """Resolve standard or generic geographic context for an edition slug."""
    slug = (edition_slug or "").strip().lower()
    name = edition_name.strip() if edition_name else slug.capitalize()

    if slug == "berdyansk":
        return EditionGeographyContext(
            edition_slug="berdyansk",
            edition_name=name or "Бердянск",
            target_locations=_BERDYANSK_TARGET_LOCATIONS,
            district_locations=_BERDYANSK_DISTRICT_LOCATIONS,
            region_name="Запорожская область",
            neighboring_regions=("Донецкая область", "Херсонская область", "Ростовская область"),
            out_of_scope_locations=_COMMON_OUT_OF_SCOPE_LOCATIONS,
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
