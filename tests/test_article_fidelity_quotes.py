# ruff: noqa: S101
"""W4: masked words, direct-quote fragments and profanity in article quotes and prose."""

from __future__ import annotations

from types import SimpleNamespace

from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_quote_allowlist import build_article_quote_allowlist
from src.publication.article_quote_masks import (
    contains_profanity,
    is_masked_swear,
    mask_safe_quote_candidate,
    quote_mask_quality_findings,
    quote_starts_after_mask,
    split_sentences_outside_masks,
)

MASKED_SOURCE = "Б... Дь а котельная как не работала, так и не работает."
MASKED_FRAGMENT = "а котельная как не работала, так и не работает"


def _sup(support_id: str, text: str, source: str) -> SimpleNamespace:
    return SimpleNamespace(
        support_id=support_id,
        text=text,
        source_text=source,
        publication_use="PUBLISH",
        evidence_kind="community_report",
        support_kind="evidence",
        story_id="story:1",
        temporal_role="CURRENT_WINDOW",
        reply_parent_context_text="",
    )


def _allowlist(*supports: SimpleNamespace) -> tuple[str, ...]:
    return build_article_quote_allowlist(SimpleNamespace(support_index=supports))


def _draft(text: str, support_ids: tuple[str, ...]) -> StructuredArticleDraft:
    paragraph = ArticleParagraph(
        text=text,
        cited_support_ids=support_ids,
        claims=(ArticleClaimAtom(text=text, cited_support_ids=support_ids),),
    )
    return StructuredArticleDraft(
        title="Тепло",
        title_support_ids=(),
        lead="Вводная часть.",
        lead_support_ids=(),
        sections=(ArticleSection(heading="Отопление", paragraphs=(paragraph,)),),
    )


def _findings(text: str, source: str, support_id: str = "s1") -> list[object]:
    support = _sup(support_id, source, source)
    context = SimpleNamespace(support_by_id={support_id: support})
    return list(quote_mask_quality_findings(_draft(text, (support_id,)), context))


def _codes(text: str, source: str) -> list[str]:
    return [finding.code for finding in _findings(text, source)]


# ---- W4.1: masked words and quote splitting ------------------------------------------


def test_sentence_split_never_breaks_inside_a_masked_word():
    text = "Б... Дь а котельная как не работала. Вода течёт."

    pieces = split_sentences_outside_masks(text)

    assert len(pieces) == 2
    assert pieces[0].startswith("Б... Дь")
    assert pieces[1].strip() == "Вода течёт."


def test_candidate_cut_to_whole_words_after_a_mask_in_the_source():
    candidate = "Б... Дь, а котельная как не работала, так и не работает."

    cut = mask_safe_quote_candidate(candidate, MASKED_SOURCE)

    assert cut == MASKED_FRAGMENT
    assert "Б..." not in cut


def test_candidate_starting_inside_a_source_mask_is_cut_to_the_next_word():
    candidate = "Дь а котельная как не работала, так и не работает."

    cut = mask_safe_quote_candidate(candidate, MASKED_SOURCE)

    assert cut == MASKED_FRAGMENT


def test_allowlist_keeps_the_whole_word_fragment_and_drops_the_mask_tail():
    allowed = _allowlist(
        _sup("s1", "Дь а котельная как не работала, так и не работает.", MASKED_SOURCE)
    )

    assert MASKED_FRAGMENT in allowed
    assert not any(quote.startswith("Дь") for quote in allowed)
    assert not any("Б..." in quote for quote in allowed)


def test_allowlist_does_not_treat_a_mask_as_a_sentence_boundary():
    # The sentence split of the writer projection must not cut inside «Б... Дь».
    source = "Б... Дь а котельная как не работала. Вода течёт ровной струёй."
    support = _sup("s1", source, source)
    context = SimpleNamespace(support_index=(support,))

    allowed = build_article_quote_allowlist(context, candidate_text_by_support_id={"s1": source})

    assert "Вода течёт ровной струёй." in allowed
    assert "а котельная как не работала" in allowed
    assert not any(quote.startswith("Дь") or quote.startswith("Б...") for quote in allowed)


# ---- W4.2: profanity is never allowlisted or published --------------------------------


def test_profanity_is_never_allowlisted_while_a_clean_sentence_is_kept():
    clean = "Вода течёт ровной струёй"
    dirty = "Ну и блядь, вода течёт ржавая"

    allowed = _allowlist(_sup("s1", dirty, dirty), _sup("s2", clean, clean))

    assert clean in allowed
    assert not any(contains_profanity(quote) for quote in allowed)


def test_profanity_detector_covers_explicit_and_masked_swear_words():
    assert contains_profanity("Ну и блядь, вода течёт")
    assert contains_profanity("вода течёт, Б... Дь")
    assert is_masked_swear("х", "й")
    assert not contains_profanity("Вода течёт ровной струёй")


def test_candidate_with_profanity_after_cut_is_dropped():
    assert mask_safe_quote_candidate("Ну и блядь, вода течёт", "Ну и блядь, вода течёт") is None
    assert mask_safe_quote_candidate("Вода течёт ровно", "Вода течёт ровно") == "Вода течёт ровно"


def test_profanity_in_prose_is_a_repair_finding():
    source = "Жители сообщили, что вода ржавая, блядь."
    assert _codes(source, source) == ["PROFANITY_IN_ARTICLE"]


def test_masked_profanity_in_prose_is_a_repair_finding():
    source = "Жители сообщили, что вода ржавая, Б... Дь."
    assert _codes(source, source) == ["PROFANITY_IN_ARTICLE"]


def test_clean_prose_gets_no_profanity_finding():
    source = "Жители сообщили, что вода ржавая."
    assert _codes(source, source) == []


def test_profanity_finding_is_repair_severity_not_blocking():
    source = "Жители сообщили, что вода ржавая, блядь."

    findings = _findings(source, source)

    assert [finding.severity for finding in findings] == ["repair"]


# ---- W4.2: QUOTE_STARTS_AFTER_MASK ----------------------------------------------------


def test_quote_that_starts_right_after_a_source_mask_is_flagged():
    text = f"Жительница сообщила: «{MASKED_FRAGMENT}»."

    assert quote_starts_after_mask(MASKED_FRAGMENT, MASKED_SOURCE) is True
    assert _codes(text, MASKED_SOURCE) == ["QUOTE_STARTS_AFTER_MASK"]


def test_quote_from_a_source_without_a_mask_is_not_flagged():
    source = "Жительница сообщила, а котельная как не работала, так и не работает."
    text = f"Жительница сообщила: «{MASKED_FRAGMENT}»."

    assert quote_starts_after_mask(MASKED_FRAGMENT, source) is False
    assert _codes(text, source) == []


def test_quote_finding_is_repair_severity_and_names_the_unit():
    text = f"Жительница сообщила: «{MASKED_FRAGMENT}»."

    findings = _findings(text, MASKED_SOURCE)

    assert [finding.severity for finding in findings] == ["repair"]
    assert findings[0].unit_id == "P001"
