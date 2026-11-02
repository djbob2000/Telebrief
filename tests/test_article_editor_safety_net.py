# ruff: noqa: S101
"""Editor response tolerance, sentence-level backstop and second-call targeting."""

from __future__ import annotations

import json
from dataclasses import replace

from src.publication.article_editor import (
    ArticleEditor,
    _has_blocking_issue,
    _read_only_article_block,
    _trim_unsupported_sentences,
)
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_validator import (
    ArticleValidationIssue,
    ArticleValidationResult,
)

REQUESTED = {"P001", "P002"}


def _parse(payload: object) -> tuple[dict[str, str], str, int]:
    return ArticleEditor._parse_editor_response_details(
        json.dumps(payload, ensure_ascii=False), REQUESTED
    )


def test_units_as_list_of_id_tagged_objects_are_applied():
    patches, reason, _ = _parse(
        {"units": [{"unit_id": "P001", "text": "Свет дали в нагорной части."}]}
    )

    assert reason == "completed"
    assert patches == {"P001": "Свет дали в нагорной части."}


def test_alternative_text_key_inside_unit_object_is_accepted():
    patches, reason, _ = _parse({"units": {"P002": {"new_text": "Вода есть по адресу."}}})

    assert reason == "completed"
    assert patches == {"P002": "Вода есть по адресу."}


def test_malformed_value_is_skipped_while_valid_patch_survives():
    patches, reason, _ = _parse(
        {"units": {"P001": ["not", "text"], "P002": "Текст, который можно применить."}}
    )

    assert reason == "completed"
    assert patches == {"P002": "Текст, который можно применить."}


def test_envelope_without_any_usable_unit_is_still_invalid_shape():
    patches, reason, _ = _parse({"units": {"P001": ["only", "lists"]}})

    assert reason == "invalid_response_shape"
    assert patches == {}


def test_top_level_list_is_invalid_shape_without_leaking_prose():
    patches, reason, _ = _parse(["Текст статьи, который не должен попасть в лог."])

    assert reason == "invalid_response_shape"
    assert patches == {}


def _draft(*paragraphs: str) -> StructuredArticleDraft:
    return StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("s1",),
        lead="Лид.",
        lead_support_ids=("s1",),
        sections=(
            ArticleSection(
                heading="Раздел",
                heading_support_ids=("s1",),
                paragraphs=tuple(
                    ArticleParagraph(
                        text=text,
                        cited_support_ids=("s1",),
                        claims=(ArticleClaimAtom(text=text, cited_support_ids=("s1",)),),
                    )
                    for text in paragraphs
                ),
            ),
        ),
    )


def _result(*issues: ArticleValidationIssue) -> ArticleValidationResult:
    return ArticleValidationResult(is_valid=False, word_count=0, section_count=1, issues=issues)


def _issue(code: str, unit_id: str, claim_text: str | None) -> ArticleValidationIssue:
    return ArticleValidationIssue(
        code=code,
        unit_id=unit_id,
        message="finding",
        claim_text=claim_text,
    )


def test_only_the_flagged_sentence_is_removed_and_citations_are_kept():
    draft = _draft("Свет в нагорной части дали утром. Мост на улице Ленина закрыт до пятницы.")
    result = _result(_issue("MISSING_CLAIM_SUPPORT", "P001", "Мост закрыт до пятницы"))

    outcome = _trim_unsupported_sentences(draft, result)

    assert outcome is not None
    trimmed, removed = outcome
    assert removed == {"P001": 1}
    paragraph = trimmed.sections[0].paragraphs[0]
    assert paragraph.text == "Свет в нагорной части дали утром."
    assert paragraph.cited_support_ids == ("s1",)
    assert [claim.text for claim in paragraph.claims] == ["Свет в нагорной части дали утром."]


def test_unflagged_paragraphs_are_left_identical():
    draft = _draft("Первый абзац без замечаний.", "Второй абзац. Мост закрыт до пятницы.")
    result = _result(_issue("UNSUPPORTED_CLAIM_ATOM", "P002", "Мост закрыт до пятницы"))

    outcome = _trim_unsupported_sentences(draft, result)

    assert outcome is not None
    trimmed, _ = outcome
    assert trimmed.sections[0].paragraphs[0] == draft.sections[0].paragraphs[0]
    assert trimmed.sections[0].paragraphs[1].text == "Второй абзац."


def test_paragraph_is_never_emptied():
    draft = _draft("Мост закрыт до пятницы.")
    result = _result(_issue("MISSING_CLAIM_SUPPORT", "P001", "Мост закрыт до пятницы"))

    assert _trim_unsupported_sentences(draft, result) is None


def test_unmatched_finding_returns_none_so_the_draft_is_not_touched():
    draft = _draft("Свет дали утром. Вода есть по всему району.")
    result = _result(_issue("MISSING_CLAIM_SUPPORT", "P001", "Газ подключили в пятницу"))

    assert _trim_unsupported_sentences(draft, result) is None


def test_non_factual_codes_are_not_trimmed():
    draft = _draft("Свет дали утром. Мост закрыт до пятницы.")
    result = _result(_issue("PHANTOM_HEADING_TOPIC", "P001", "Мост закрыт до пятницы"))

    assert _trim_unsupported_sentences(draft, result) is None


def test_unit_without_claim_text_uses_unsupported_claim_raw_text():
    from src.publication.article_claims import ConcreteClaim

    draft = _draft("Свет дали утром. Насос сломан с понедельника.")
    claim = ConcreteClaim(
        kind="causal", raw="Насос сломан с понедельника", normalized="", excerpt=""
    )
    issue = ArticleValidationIssue(
        code="UNSUPPORTED_CONCRETE_CLAIM",
        unit_id="P001",
        message="finding",
        unsupported_claims=(claim,),
    )

    outcome = _trim_unsupported_sentences(draft, _result(issue))

    assert outcome is not None
    assert outcome[0].sections[0].paragraphs[0].text == "Свет дали утром."


def test_second_call_targets_only_blocking_units():
    from src.publication.article_quality import ArticleReaderQualityFinding

    blocking = _issue("MISSING_CLAIM_SUPPORT", "P001", "text")
    non_blocking = ArticleValidationIssue(
        code="THEME_MISMATCHED_SECTION", unit_id="P002", message="x", blocking=False
    )
    roster = ArticleReaderQualityFinding(
        code="MULTI_SENTENCE_ADDRESS_STATUS_ROSTER", unit_id="P003", message="x"
    )
    repetition = ArticleReaderQualityFinding(
        code="CROSS_SECTION_REPETITION", unit_id="P004", message="x"
    )

    assert _has_blocking_issue({"issues": (blocking,)}) is True
    assert _has_blocking_issue({"issues": (roster,)}) is True
    assert _has_blocking_issue({"issues": (non_blocking,)}) is False
    assert _has_blocking_issue({"issues": (repetition,)}) is False
    assert _has_blocking_issue({"issues": ()}) is False


def test_bracketed_or_lowercase_unit_ids_are_accepted():
    patches, reason, _ = _parse({"units": {"[P001]": "Свет дали.", "p002": "Вода есть."}})

    assert reason == "completed"
    assert patches == {"P001": "Свет дали.", "P002": "Вода есть."}


def test_unrequested_unit_is_ignored_while_requested_patch_survives():
    patches, reason, unknown = _parse({"units": {"P001": "Свет дали.", "P009": "Чужой абзац."}})

    assert reason == "unknown_unit_ids"
    assert unknown == 1
    assert patches == {"P001": "Свет дали."}


def test_read_only_article_lists_every_unit_with_its_positional_id():
    draft = _draft("Первый абзац.", "Второй абзац.")

    block = _read_only_article_block(draft)
    article = json.loads(block[block.index("{") :])

    assert article["TITLE"] == "Заголовок"
    assert article["LEAD"] == "Лид."
    assert article["sections"][0]["section_id"] == "H001"
    assert [p["unit_id"] for p in article["sections"][0]["paragraphs"]] == ["P001", "P002"]
    assert [p["text"] for p in article["sections"][0]["paragraphs"]] == [
        "Первый абзац.",
        "Второй абзац.",
    ]


def _section_draft(heading: str, *paragraphs: str) -> StructuredArticleDraft:
    draft = _draft(*paragraphs)
    section = draft.sections[0]
    return replace(
        draft,
        sections=(
            replace(
                section,
                heading=heading,
                heading_claims=(ArticleClaimAtom(text=heading, cited_support_ids=("s1",)),),
            ),
        ),
    )


def test_phantom_heading_clause_is_removed_and_body_is_kept():
    draft = _section_draft(
        "Нагорная часть: свет, вода и газ",
        "Свет дали утром. Вода есть по всему району.",
    )
    result = _result(_issue("PHANTOM_HEADING_TOPIC", "H001", None))

    outcome = _trim_unsupported_sentences(draft, result)

    assert outcome is not None
    trimmed, removed = outcome
    assert removed == {"H001": 1}
    section = trimmed.sections[0]
    assert section.heading == "Нагорная часть: свет и вода"
    assert section.heading_claims[0].text == "Нагорная часть: свет и вода"
    assert section.heading_claims[0].cited_support_ids == ("s1",)
    assert section.paragraphs == draft.sections[0].paragraphs


def test_heading_left_without_clauses_keeps_only_its_topic():
    draft = _section_draft("Нагорная часть: газ", "Свет дали утром.")
    result = _result(_issue("PHANTOM_HEADING_TOPIC", "H001", None))

    outcome = _trim_unsupported_sentences(draft, result)

    assert outcome is not None
    assert outcome[0].sections[0].heading == "Нагорная часть"


def test_heading_whose_clauses_all_appear_in_body_is_not_touched():
    draft = _section_draft("Свет и вода", "Свет дали утром. Вода есть по всему району.")
    result = _result(_issue("PHANTOM_HEADING_TOPIC", "H001", None))

    assert _trim_unsupported_sentences(draft, result) is None


def test_lead_without_current_evidence_is_offered_current_body_supports():
    from unittest.mock import MagicMock

    from src.publication.article_context import ArticleEditorialContext, ArticleSupport

    def support(sid: str, text: str, role: str) -> ArticleSupport:
        return ArticleSupport(
            support_id=sid,
            text=text,
            source_text=text,
            support_kind="evidence",
            publication_use="PUBLISH",
            source_refs=(),
            fragment_ids=(),
            source_item_ids=(),
            observed_at=None,
            temporal_role=role,
            evidence_kind="community_report",
            story_id=f"story:{sid}",
        )

    old = support("h", "В октябре на Азмоле не было света.", "HISTORICAL_CONTEXT")
    now = support("c", "1 ноября на Азмоле света не было.", "CURRENT_WINDOW")
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=(old, now),
        support_by_id={"h": old, "c": now},
        recurring_topics=(),
    )
    draft = StructuredArticleDraft(
        title="Свет",
        title_support_ids=("c",),
        lead="На Азмоле не было света.",
        lead_support_ids=("h",),
        sections=(
            ArticleSection(
                heading="Азмол",
                heading_support_ids=("c",),
                paragraphs=(
                    ArticleParagraph(
                        text="1 ноября на Азмоле света не было.",
                        cited_support_ids=("c",),
                        claims=(
                            ArticleClaimAtom(
                                text="1 ноября на Азмоле света не было.",
                                cited_support_ids=("c",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    issue = ArticleValidationIssue(
        code="INVALID_SUPPORT_POLICY", unit_id="LEAD", message="x", support_ids=("h",)
    )

    editor = ArticleEditor(provider=MagicMock(), model="anthropic/claude-haiku-5.5")
    units = editor._build_unit_contexts(draft, {"LEAD": [issue]}, context, material_projection=None)

    assert units[0]["unit_id"] == "LEAD"
    assert units[0]["support_ids"] == ["h", "c"]
