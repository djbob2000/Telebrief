# ruff: noqa: S101
"""article-fidelity W2.1: a concrete value needs its own owning support.

Fixtures are the real message texts of the 12 Nov article; author names are not used.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_geography import _load_place_resolver
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_validator import validate_article_draft
from src.publication.article_value_binding import (
    CONCRETE_VALUE_MISBOUND,
    check_concrete_value_binding,
    clause_values,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE = REPO_ROOT / "data/city_profiles/berdyansk.yaml"


def _support(
    sid: str,
    text: str,
    source: str,
    *,
    kind: str = "community_report",
    use: str = "PUBLISH",
    reply_parent: str = "",
) -> ArticleSupport:
    return ArticleSupport(
        support_id=sid,
        text=text,
        source_text=source,
        support_kind="evidence",
        publication_use=use,  # type: ignore[arg-type]
        source_refs=("src",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=None,
        temporal_role="CURRENT_WINDOW",
        evidence_kind=kind,
        reply_parent_context_text=reply_parent,
    )


QUESTION_612180 = _support(
    "story:1:evidence:0:frag:612180",
    "Вы звонили в теплосеть? Че говорят?",
    "Вы звонили в теплосеть? Че говорят?",
    kind="resident_question",
    use="CONTEXT",
)
POWER_639128 = _support(
    "story:1:evidence:1:frag:639128",
    "По сообщению жителя, свет выключили ещё около 2 часов дня.",
    "Нет. Еще часа в 2 дня выключили",
    reply_parent="На АЗМОЛЕ ещё есть свет?",
)
DISPATCH_640498 = _support(
    "story:1:evidence:2:frag:640498",
    "По сообщению жительницы, дозвониться в диспетчерскую удалось только на третий день, "
    "и она не видит смысла звонить туда снова.",
    "Сегодня на третий день дозвонилась. Смысла не вижу туда звонить",
)
EIGHT_MARCH_640451 = _support(
    "story:2:evidence:0:frag:640451",
    "По сообщению жительницы, подачу дали в 11 до 15, со слов соседей.",
    "Это шикарно, у нас не было 9 дней, сегодня дали в 11до 15,со слов соседей, дозвонилась в рес…",
)
LISKI_NOTE = _support(
    "story:3:evidence:0:frag:700001",
    "В Лисках свет выключили в 10 утра.",
    "В Лисках свет выключили в 10 утра.",
)

ALL_SUPPORTS = (
    QUESTION_612180,
    POWER_639128,
    DISPATCH_640498,
    EIGHT_MARCH_640451,
    LISKI_NOTE,
)


@pytest.fixture
def repo_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    # The validator resolves the edition profile relative to the working directory.
    monkeypatch.chdir(REPO_ROOT)


def _context(supports: tuple[ArticleSupport, ...], slug: str = "") -> ArticleEditorialContext:
    return ArticleEditorialContext(
        headline_candidates=(),
        support_index=supports,
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
        edition_slug=slug,
    )


def _paragraph_draft(text: str, cited: tuple[str, ...]) -> StructuredArticleDraft:
    return StructuredArticleDraft(
        title="Заголовок статьи",
        title_support_ids=(POWER_639128.support_id,),
        lead="Лид статьи.",
        lead_support_ids=(POWER_639128.support_id,),
        sections=(
            ArticleSection(
                heading="Свет и отопление",
                heading_support_ids=(POWER_639128.support_id,),
                paragraphs=(
                    ArticleParagraph(
                        text=text,
                        cited_support_ids=cited,
                        claims=(ArticleClaimAtom(text=text, cited_support_ids=cited),),
                    ),
                ),
            ),
        ),
    )


def _misbound(result: object) -> list[object]:
    return [i for i in result.issues if i.code == CONCRETE_VALUE_MISBOUND]  # type: ignore[attr-defined]


def test_clause_values_read_word_numerals_only_before_units() -> None:
    assert clause_values("Линия теплосети занята с двух часов дня") == ("2",)
    assert clause_values("только на третий день") == ("3",)
    assert clause_values("одна из жительниц смогла пробиться") == ()
    assert clause_values("сегодня дали в 11до 15") == ("11", "15")
    assert clause_values("свет выключили в 10:00") == ("10:00",)


def test_heating_clause_with_a_power_owner_is_caught(repo_cwd: None) -> None:
    text = (
        "Линия теплосети занята с двух часов дня, а одна из жительниц смогла пробиться "
        "в диспетчерскую только на третий день."
    )
    cited = (QUESTION_612180.support_id, POWER_639128.support_id, DISPATCH_640498.support_id)
    result = validate_article_draft(_paragraph_draft(text, cited), _context(ALL_SUPPORTS))
    findings = _misbound(result)
    assert len(findings) == 1
    assert findings[0].blocking is True  # type: ignore[attr-defined]
    # The one-sentence backstop acts on the sentence named by claim_text.
    assert findings[0].claim_text == text  # type: ignore[attr-defined]
    assert findings[0].unit_id == "P001"  # type: ignore[attr-defined]
    assert POWER_639128.support_id in findings[0].support_ids  # type: ignore[attr-defined]


def test_value_owned_by_its_own_power_source_passes(repo_cwd: None) -> None:
    # Азмол is named only in the reply parent of 639128, which never owns a place.
    text = "На Азмоле свет выключили около двух часов дня."
    result = validate_article_draft(
        _paragraph_draft(text, (POWER_639128.support_id,)),
        _context(ALL_SUPPORTS, slug="berdyansk"),
    )
    assert _misbound(result) == []
    assert any(
        code.startswith("CONCRETE_VALUE_NOT_EVALUATED:P001:2:") for code in result.not_evaluated
    )


def test_third_day_clause_owned_by_its_own_source_passes(repo_cwd: None) -> None:
    text = "Одна из жительниц смогла пробиться в диспетчерскую только на третий день."
    result = validate_article_draft(
        _paragraph_draft(text, (DISPATCH_640498.support_id,)),
        _context(ALL_SUPPORTS),
    )
    assert _misbound(result) == []


def test_unowned_values_without_service_are_not_evaluated_not_verified() -> None:
    # 640451 has no own place and no service family the clause can be compared with.
    text = (
        "Утром 11 ноября свет там включили в 10:00 и выключили в 16:15; "
        "в другой день подача шла с 11 до 15."
    )
    result = check_concrete_value_binding("P002", text, [EIGHT_MARCH_640451])
    assert result.findings == ()
    codes = {d.value: d.reason for d in result.diagnostics}
    assert codes["10:00"] == "no_owner"
    assert codes["11"] == "service_undetermined"


def test_place_named_only_in_own_text_of_another_municipal_area_is_caught(repo_cwd: None) -> None:
    resolver = _load_place_resolver(str(PROFILE))
    assert resolver is not None
    # The clause names Лиски; the owning message names Азмол in its own text.
    azmol_own = _support(
        "story:4:evidence:0:frag:700002",
        "По сообщению жителя, на Азмоле свет выключили в 10 утра.",
        "На Азмоле свет выключили в 10 утра.",
    )
    text = "В Лисках свет выключили в 10 утра."
    result = check_concrete_value_binding("P003", text, [azmol_own], place_resolver=resolver)
    assert [f.code for f in result.findings] == [CONCRETE_VALUE_MISBOUND]
    assert result.findings[0].claim_text == text
    assert result.findings[0].support_ids == (azmol_own.support_id,)


def test_same_place_in_own_text_is_compatible(repo_cwd: None) -> None:
    resolver = _load_place_resolver(str(PROFILE))
    assert resolver is not None
    own = _support(
        "story:4:evidence:0:frag:700003",
        "По сообщению жителя, в Лисках свет выключили в 10 утра.",
        "В Лисках свет выключили в 10 утра.",
    )
    result = check_concrete_value_binding(
        "P003", "В Лисках свет выключили в 10 утра.", [own], place_resolver=resolver
    )
    assert result.findings == ()
