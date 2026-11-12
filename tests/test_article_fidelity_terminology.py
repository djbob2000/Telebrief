"""Article-fidelity W6.4: profile-declared edition terminology."""

# ruff: noqa: S101
import datetime as dt

from src.domain.edition_terminology import (
    apply_edition_terminology,
    canonical_term_fields,
    load_edition_terminology,
    parse_edition_terminology,
    standardize_edition_terminology,
)
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_finalization import prepare_article_draft
from src.publication.article_models import ArticleParagraph, ArticleSection, StructuredArticleDraft
from src.publication.article_validator import _official_area_source_texts

OBSERVED = dt.datetime(2026, 11, 11, 12, 0, tzinfo=dt.timezone.utc)


def test_profile_variant_is_corrected_in_reader_text_keeping_the_ending():
    text = "ждёт воду до окончания перефрезки водоводов"
    assert (
        standardize_edition_terminology(text, "berdyansk")
        == "ждёт воду до окончания переврезки водоводов"
    )


def test_every_inflection_and_capitalisation_keeps_its_ending():
    assert standardize_edition_terminology("Перефрезка водоводов", "berdyansk") == (
        "Переврезка водоводов"
    )
    assert standardize_edition_terminology("после перефрезке", "berdyansk") == ("после переврезке")


def test_direct_quote_keeps_the_variant_exactly():
    text = "Житель написал: «перефрезка водоводов до вечера»."
    assert standardize_edition_terminology(text, "berdyansk") == text


def test_prose_outside_the_quote_is_corrected_around_a_quote():
    text = "Житель написал: «перефрезка водоводов», и ждёт перефрезки."
    assert standardize_edition_terminology(text, "berdyansk") == (
        "Житель написал: «перефрезка водоводов», и ждёт переврезки."
    )


def test_canonical_spelling_is_left_unchanged():
    """Faithful case: the correct term is never rewritten."""
    text = "ждёт переврезки водоводов"
    assert standardize_edition_terminology(text, "berdyansk") == text


def test_word_boundary_protects_longer_words():
    assert standardize_edition_terminology("перефрезкой", "berdyansk") == "переврезкой"
    assert standardize_edition_terminology("супер-перефрезка", "berdyansk") == ("супер-переврезка")
    assert standardize_edition_terminology("неперефрезка", "berdyansk") == "неперефрезка"


def test_profile_without_terminology_changes_nothing():
    assert load_edition_terminology("no-such-edition") == ()
    assert standardize_edition_terminology("перефрезки", "no-such-edition") == "перефрезки"


def test_malformed_entries_are_ignored_not_applied():
    terms = parse_edition_terminology(
        [
            {"variant": "перефрезк", "canonical": "переврезк"},
            {"variant": "", "canonical": "x"},
            {"variant": "abc", "canonical": "abc"},
            {"variant": "го", "canonical": "гол"},
            "not-a-mapping",
            {"variant": "перефрезк", "canonical": "другой"},
        ]
    )
    assert [(term.variant, term.canonical) for term in terms] == [("перефрезк", "переврезк")]
    assert apply_edition_terminology("перефрезки", ()) == "перефрезки"


def test_writer_dossier_gets_the_canonical_term_only_when_the_variant_occurs():
    assert canonical_term_fields("berdyansk", "Ждут окончания перефрезки.") == [
        "перефрезк -> переврезк"
    ]
    assert canonical_term_fields("berdyansk", "Ждут окончания переврезки.") == []


def _support(text: str) -> ArticleSupport:
    return ArticleSupport(
        support_id="s1",
        text=text,
        source_text=text,
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("telegram:source:1:item:1:rev:1:frag:1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=OBSERVED,
        evidence_kind="community_report",
        source_roles=("community",),
        story_id="story:1",
    )


def test_article_preparation_applies_terminology_outside_quotes():
    support = _support("Жители ждут окончания перефрезки водоводов.")
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=(support,),
        support_by_id={support.support_id: support},
        recurring_topics=(),
        edition_slug="berdyansk",
    )
    draft = StructuredArticleDraft(
        title="Вода до окончания перефрезки",
        title_support_ids=("s1",),
        lead="Жители ждут окончания перефрезки водоводов.",
        lead_support_ids=("s1",),
        sections=(
            ArticleSection(
                heading="Вода",
                paragraphs=(
                    ArticleParagraph(
                        text="Житель написал: «перефрезка водоводов» — и ждёт перефрезки.",
                        cited_support_ids=("s1",),
                    ),
                ),
            ),
        ),
    )

    prepared = prepare_article_draft(
        draft, context=context, material_projection=None, place_resolver=None
    )

    assert prepared.lead == "Жители ждут окончания переврезки водоводов."
    assert prepared.sections[0].paragraphs[0].text == (
        "Житель написал: «перефрезка водоводов» — и ждёт переврезки."
    )


def test_validator_accepts_the_canonical_spelling_for_a_variant_source():
    """The prose says «переврезки»; the source said «перефрезки». Both must match."""
    official = _official_area_source_texts([_support("ждут окончания перефрезки")], "berdyansk")
    assert official == ["ждут окончания переврезки"]
    assert _official_area_source_texts([_support("ждут окончания переврезки")], "berdyansk") == []
