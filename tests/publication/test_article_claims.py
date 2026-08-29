"""Unit tests for concrete claim extraction and normalization."""

from __future__ import annotations

import pytest

from src.publication.article_claims import (
    extract_concrete_claims,
    find_unsupported_claims,
    normalize_support_text,
)


@pytest.mark.unit
def test_extract_concrete_claims_kinds() -> None:
    def kinds(text: str) -> set[str]:
        return {c.kind for c in extract_concrete_claims(text)}

    assert "number" in kinds("в течение полутора часов")
    assert kinds("до 10–12 минут") >= {"number", "time"}
    assert kinds("до конца сентября") == {"date"}
    assert kinds("за 27 000 руб.") >= {"number", "money"}
    assert kinds("+7 (990) 024-06-35") == {"phone"}
    assert kinds("выставка «Морские горизонты»") == {"quoted_name"}
    assert kinds("сеть ONET работает") == {"acronym"}
    assert "causal_relation" in kinds("отключение вызвано аварией на подстанции")
    assert "causal_relation" in kinds("подача прекратилась из-за гидроудара")
    assert "mechanism_relation" in kinds("питание восстановили по резервной схеме")


@pytest.mark.unit
def test_normalization_equivalence() -> None:
    norm1 = normalize_support_text("30 августа")
    norm2 = normalize_support_text("30.08")
    assert "30.08" in norm1 or "30 августа" in norm2 or "30.08" in norm2

    norm_range1 = normalize_support_text("10–12 минут")
    norm_range2 = normalize_support_text("10-12 минут")
    assert norm_range1 == norm_range2


@pytest.mark.unit
def test_find_unsupported_claims() -> None:
    # 1. Number & time: "полтора часа" unsupported when support only says "временно обесточена"
    support = [
        "Авария на подстанции: временно обесточена центральная часть Бердянска. "
        "Бригада РЭС ведет восстановительные работы."
    ]
    unsupported = find_unsupported_claims(
        "Бригады восстановили питание в течение полутора часов.", support
    )
    assert any(c.kind in ("number", "time") for c in unsupported)

    # 2. Mechanism: "по резервной схеме" unsupported
    unsupported_mech = find_unsupported_claims(
        "Энергоснабжение восстановили по резервной схеме.", support
    )
    assert any(c.kind == "mechanism_relation" for c in unsupported_mech)

    # 3. Causal: "из-за гидроудара" unsupported when support only says "повторный порыв"
    support_water = ["Вечером снова нет воды из-за повторного порыва на магистрали."]
    unsupported_cause = find_unsupported_claims(
        "Давление в сети упало из-за гидроудара.", support_water
    )
    assert any(c.kind == "causal_relation" for c in unsupported_cause)

    # 4. Bus intervals: "до 10-12 минут" unsupported when support only says "10 новых автобусов"
    support_bus = ["10 новых автобусов вышли на маршруты №4, 17 и 21."]
    unsupported_bus = find_unsupported_claims(
        "Интервалы движения сократились до 10–12 минут.", support_bus
    )
    assert any(c.kind in ("number", "time") for c in unsupported_bus)

    # 5. Exhibition end date: "до конца сентября" unsupported when support says "до конца месяца" (in August)
    support_art = ["В художественном музее открылась выставка картин местных авторов."]
    unsupported_art = find_unsupported_claims(
        "Экспозиция будет работать до конца сентября.", support_art
    )
    assert any(c.kind == "date" for c in unsupported_art)


@pytest.mark.unit
def test_supported_paraphrase_passes() -> None:
    support = [
        "Авария на подстанции: временно обесточена центральная и нагорная часть Бердянска. "
        "Бригада РЭС ведет восстановительные работы."
    ]
    draft = "В центре и на Нагорной части произошло отключение из-за аварии на подстанции."
    unsupported = find_unsupported_claims(draft, support)
    assert len(unsupported) == 0

    support_gas = ["30 августа с 08:00 до 17:00 будет прекращена подача газа."]
    draft_gas = "30 августа газ планово отключат с 08:00 до 17:00."
    unsupported_gas = find_unsupported_claims(draft_gas, support_gas)
    assert len(unsupported_gas) == 0
