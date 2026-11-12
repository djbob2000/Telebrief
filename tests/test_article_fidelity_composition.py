"""Article-fidelity W5 (topics, headings, conflicting accounts) and writer rules (W8).

Fixtures are the message texts of the 12 Nov 2026 defects without author names.
"""

# ruff: noqa: S101
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.city_context import CityContextResolver
from src.publication import article_quality as aq
from src.publication.article_composition_checks import (
    ARTICLE_FIDELITY_WRITER_RULES,
    find_unframed_conflicting_accounts,
)
from src.publication.article_context import (
    ArticleEditorialContext,
    ArticleSupport,
    _with_context_theme_hints,
    article_support_theme_hints,
)
from src.publication.article_models import (
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_quality_policy import ARTICLE_QUALITY_FINDING_POLICIES
from src.publication.evidence import ServiceSubjectHint
from src.publication.narrative_contract import build_article_narrative_contract

PROFILE = Path(__file__).resolve().parents[1] / "data/city_profiles/berdyansk.yaml"
OBSERVED = dt.datetime(2026, 11, 11, 18, 0, tzinfo=dt.timezone.utc)
HEADING_UPLAND = "Частный сектор и Гора: недели без света"


@pytest.fixture(scope="module")
def resolver():
    return aq._DiagnosisPlaceResolver(CityContextResolver.from_yaml(PROFILE))


def _support(
    support_id: str,
    text: str,
    *,
    story: str = "story:1",
    hint: ServiceSubjectHint | None = None,
    parent: str = "",
    items: tuple[int, ...] = (1,),
) -> ArticleSupport:
    return ArticleSupport(
        support_id=support_id,
        text=text,
        source_text=text,
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=(),
        fragment_ids=(1,),
        source_item_ids=items,
        observed_at=OBSERVED,
        evidence_kind="community_report",
        source_roles=("community",),
        story_id=story,
        service_subject_hint=hint,
        reply_parent_context_text=parent,
    )


def _section_draft(heading: str, *paragraph_texts: str, supports=("s1",)) -> StructuredArticleDraft:
    return StructuredArticleDraft(
        title="Бердянск",
        title_support_ids=(),
        lead="Лид.",
        lead_support_ids=(),
        sections=(
            ArticleSection(
                heading=heading,
                paragraphs=tuple(
                    ArticleParagraph(text=text, cited_support_ids=tuple(supports))
                    for text in paragraph_texts
                ),
            ),
        ),
    )


def _context(*supports: ArticleSupport) -> ArticleEditorialContext:
    return ArticleEditorialContext(
        headline_candidates=(),
        support_index=tuple(supports),
        support_by_id={support.support_id: support for support in supports},
        recurring_topics=(),
        edition_slug="berdyansk",
    )


# W5.1 — service theme from the reply parent or the Story (advisory).

DISPATCHER_HEADLINE = "Диспетчер сообщил о планах включить свет 14 числа в первой половине дня"
PLAN_TEXT = (
    "Житель сообщает, что дозвонился диспетчеру и тот сказал, что включение по их линии "
    "планируют 14 числа в первой половине дня."
)
PARENT_TEXT = (
    "Частный сектор, по Димитрова, Островского и т.д., те кто сидит без света больше 20-ти "
    "дней, кто-нибудь смог дозвониться товарищу диспетчеру?"
)
REPLY_TEXT = "Сегодня на третий день дозвонилась. Смысла не вижу туда звонить"


def test_story_headline_gives_the_plan_support_a_power_hint_only_as_navigation():
    support = _support("640941", PLAN_TEXT)
    hinted = _with_context_theme_hints([support], {"story:1": DISPATCHER_HEADLINE})[0]
    assert hinted.context_theme_hint == "power"
    assert hinted.context_theme_origin == "story"
    assert article_support_theme_hints(hinted) == ("power",)
    # The fact text and evidence identity are unchanged.
    assert hinted.text == support.text
    assert hinted.support_id == support.support_id


def test_reply_parent_gives_the_reply_a_power_hint_when_it_names_one_service():
    reply = _support("640498", REPLY_TEXT, parent=PARENT_TEXT, items=(2,))
    hinted = _with_context_theme_hints([reply], {})[0]
    assert hinted.context_theme_hint == "power"
    assert hinted.context_theme_origin == "reply_parent"


def test_own_service_theme_is_never_overridden_by_context():
    own = _support("w1", "Вода не идёт с утра.", parent=PARENT_TEXT)
    hinted = _with_context_theme_hints([own], {"story:1": DISPATCHER_HEADLINE})[0]
    assert hinted is own
    assert article_support_theme_hints(hinted) == ("water",)


def test_ambiguous_context_gives_no_hint():
    """Faithful silence: a headline naming two services does not pick one."""
    support = _support("s1", PLAN_TEXT)
    hinted = _with_context_theme_hints([support], {"story:1": "Свет и вода отключены"})[0]
    assert hinted.context_theme_hint == ""
    assert article_support_theme_hints(hinted) == ()


def test_structured_subject_of_another_support_in_the_story_counts():
    other = _support(
        "s2",
        "Свет отключили на сутки.",
        hint=ServiceSubjectHint(
            subject_key="power_supply",
            subject_label="свет",
            family="power",
            state="UNAVAILABLE",
            location="",
        ),
    )
    support = _support("s1", PLAN_TEXT)
    hinted = _with_context_theme_hints([support, other], {})[0]
    assert hinted.context_theme_hint == "power"


def test_non_service_parent_words_give_no_hint():
    """A parent that names only an economy word is not a service theme."""
    support = _support("s1", REPLY_TEXT, parent="Кто-нибудь покупал там товар?")
    hinted = _with_context_theme_hints([support], {})[0]
    assert hinted.context_theme_hint == ""


# W5.1 — the existing thematic-mismatch check uses the hint.

HEATING_HEADING = "Котельные, диспетчеры и телефонные очереди"


def test_power_hint_in_a_heating_section_is_a_repair_finding(resolver):
    support = _with_context_theme_hints(
        [_support("640941", PLAN_TEXT)], {"story:1": DISPATCHER_HEADLINE}
    )[0]
    draft = _section_draft(
        HEATING_HEADING,
        "Диспетчер сообщил, что включение планируют 14 числа; котельная работает с перебоями.",
        supports=("640941",),
    )
    findings = aq._diagnose_theme_mismatched_sections(draft, None, _context(support), None)
    assert [(finding.code, finding.unit_id) for finding in findings] == [
        ("THEME_MISMATCHED_SECTION", "P001")
    ]
    assert findings[0].severity == "repair"


def test_power_hint_in_a_power_section_is_not_a_finding(resolver):
    support = _with_context_theme_hints(
        [_support("640941", PLAN_TEXT)], {"story:1": DISPATCHER_HEADLINE}
    )[0]
    draft = _section_draft(
        "Свет и диспетчеры", "Диспетчер сообщил о включении 14 числа.", supports=("640941",)
    )
    assert aq._diagnose_theme_mismatched_sections(draft, None, _context(support), None) == ()


def test_heating_support_in_a_heating_section_is_not_a_finding(resolver):
    support = _support("h1", "В котельной нет тепла с утра.")
    draft = _section_draft(HEATING_HEADING, "В котельной нет тепла с утра.", supports=("h1",))
    assert aq._diagnose_theme_mismatched_sections(draft, None, _context(support), None) == ()


def test_heating_support_in_a_utilities_section_is_not_a_finding(resolver):
    """Regression guard: a heating report still satisfies the general utilities heading."""
    support = _support("h1", "В котельной нет тепла с утра.")
    draft = _section_draft("Коммунальные службы", "В котельной нет тепла с утра.", supports=("h1",))
    assert aq._diagnose_theme_mismatched_sections(draft, None, _context(support), None) == ()


# W5.2 — an area named in the heading against the areas named in its paragraphs.


def _heading_hits(heading: str, *paragraphs: str):
    section = SimpleNamespace(
        heading=heading, paragraphs=[SimpleNamespace(text=p) for p in paragraphs]
    )
    return aq._heading_area_mismatch_places(section, _resolver())


_RESOLVER_CACHE: list = []


def _resolver():
    if not _RESOLVER_CACHE:
        _RESOLVER_CACHE.append(aq._DiagnosisPlaceResolver(CityContextResolver.from_yaml(PROFILE)))
    return _RESOLVER_CACHE[0]


def test_lower_town_area_under_upland_heading_is_caught():
    flagged, labels = _heading_hits(
        HEADING_UPLAND, "На косе, по словам жителя, света нет с 1 августа."
    )
    assert flagged == (0,)
    assert labels


def test_centre_and_lower_town_under_upland_heading_are_caught():
    flagged, _ = _heading_hits(
        HEADING_UPLAND,
        "В нижней части города, в Центре, на начале улицы Шевченко света нет 11 дней",
    )
    assert flagged == (0,)


def test_upland_places_under_upland_heading_pass():
    """Faithful case: the paragraph's places are in the heading's part of town."""
    flagged, _ = _heading_hits(
        HEADING_UPLAND,
        "На Горе, на улицах Димитрова и Островского, дома стоят без света больше 20 дней",
    )
    assert flagged == ()


def test_a_heading_without_an_area_is_not_checked():
    flagged, _ = _heading_hits(HEATING_HEADING, "В нижней части города, в Центре света нет 11 дней")
    assert flagged == ()


def test_area_inside_the_heading_part_passes_for_a_neighbourhood_on_upland():
    """Azmol lies on Gora in the profile; it must not be flagged under a Gora heading."""
    flagged, _ = _heading_hits("На Горе", "На Азмоле света нет.")
    assert flagged == ()


def test_heading_area_mismatch_is_repair_level_in_the_registry():
    assert ARTICLE_QUALITY_FINDING_POLICIES["HEADING_AREA_MISMATCH"].severity == "blocking"


# W5.3 — conflicting accounts visible from structured service_state.

AKZ_OFF = ServiceSubjectHint(
    subject_key="power_supply",
    subject_label="свет",
    family="power",
    state="UNAVAILABLE",
    location="часть АКЗ с рынком",
)
AKZ_ON = ServiceSubjectHint(
    subject_key="power_supply",
    subject_label="свет",
    family="power",
    state="AVAILABLE",
    location="вторая часть АКЗ",
)
AKZ_SPEAKER_A = "Часть акз с рынком без света, а другая часть со светом."
AKZ_SPEAKER_B = (
    "У вас и так часто свет есть. Давно очевидно, что вам его включают день через день. "
    "Вторая часть акз редко со светом сидит и хочет тоже посидеть со светом"
)


def _akz_context(on_items=(11,), off_items=(10,), on=AKZ_ON, off=AKZ_OFF):
    off_support = _support("a1", AKZ_SPEAKER_A, hint=off, items=off_items)
    on_support = _support("a2", AKZ_SPEAKER_B, hint=on, items=on_items)
    return _context(off_support, on_support)


def _conflicts(paragraph_text: str, context: ArticleEditorialContext, resolver):
    draft = _section_draft("АКЗ", paragraph_text, supports=("a1", "a2"))
    return aq._diagnose_unframed_conflicting_accounts(draft, context, None, resolver)


def test_two_sources_with_opposite_power_states_in_one_paragraph_are_flagged(resolver):
    findings = _conflicts(
        "Часть АКЗ с рынком без света, а вторая часть со светом.", _akz_context(), resolver
    )
    assert [(finding.code, finding.unit_id) for finding in findings] == [
        ("UNFRAMED_CONFLICTING_ACCOUNTS", "P001")
    ]
    assert findings[0].severity == "repair"
    assert findings[0].support_ids == ("a1", "a2")


def test_explicit_disagreement_marker_is_the_faithful_framing(resolver):
    findings = _conflicts(
        "Жители описывают по-разному: часть АКЗ с рынком без света, а другая часть со светом.",
        _akz_context(),
        resolver,
    )
    assert findings == ()


def test_same_source_item_is_not_a_conflict_of_accounts(resolver):
    """Two claims from one message are one account, not two speakers."""
    findings = _conflicts(
        "Часть АКЗ с рынком без света, а вторая часть со светом.",
        _akz_context(on_items=(10,), off_items=(10,)),
        resolver,
    )
    assert findings == ()


def test_different_locations_are_not_a_conflict_of_accounts(resolver):
    other_place = ServiceSubjectHint(
        subject_key="power_supply",
        subject_label="свет",
        family="power",
        state="AVAILABLE",
        location="на Лисках",
    )
    findings = _conflicts(
        "На АКЗ света нет, а на Лисках есть.",
        _akz_context(on=other_place),
        resolver,
    )
    assert findings == ()


def test_same_state_is_not_a_conflict(resolver):
    same = ServiceSubjectHint(
        subject_key="power_supply",
        subject_label="свет",
        family="power",
        state="UNAVAILABLE",
        location="вторая часть АКЗ",
    )
    findings = _conflicts("На АКЗ света нет у двух источников.", _akz_context(on=same), resolver)
    assert findings == ()


def test_without_structured_state_the_check_is_not_evaluated(resolver):
    findings = _conflicts(
        "Часть АКЗ с рынком без света, а вторая часть со светом.",
        _akz_context(on=None, off=None),
        resolver,
    )
    assert findings == ()


def test_detector_is_pure_and_takes_plain_inputs():
    findings = find_unframed_conflicting_accounts(
        [("P001", "Свет есть в одном месте и нет в другом.", ("x", "y"))],
        hint_for=lambda sid: AKZ_OFF if sid == "x" else AKZ_ON,
        source_items_for=lambda sid: frozenset({1}) if sid == "x" else frozenset({2}),
        location_ids=lambda text: frozenset({"akz"}),
    )
    assert [finding.code for finding in findings] == ["UNFRAMED_CONFLICTING_ACCOUNTS"]
    assert ARTICLE_QUALITY_FINDING_POLICIES["UNFRAMED_CONFLICTING_ACCOUNTS"].severity == "repair"


# W8 and the writer rules of W5.3, W7.1, W7.3 and W6.4.


def test_writer_rules_are_part_of_the_article_contract():
    contract = build_article_narrative_contract()
    assert ARTICLE_FIDELITY_WRITER_RULES in contract


def test_writer_is_told_not_to_fill_an_unnamed_service_with_another_noun():
    rule = ARTICLE_FIDELITY_WRITER_RULES
    assert "у нас не было 9 дней" in rule
    assert "дома не было девять дней" in rule
    assert "topic hint" in rule or "topic hint" in rule.casefold()
    assert "never fill the gap with another noun" in rule


def test_writer_is_told_to_frame_conflicting_accounts_and_keep_hedges_and_ordinals():
    rule = ARTICLE_FIDELITY_WRITER_RULES
    assert "по-разному" in rule
    assert "идёт четвёртая неделя" in rule
    for marker in ("похоже", "кажется", "вроде", "видимо", "говорят", "по слухам"):
        assert marker in rule
    assert "canonical_terms" in rule
