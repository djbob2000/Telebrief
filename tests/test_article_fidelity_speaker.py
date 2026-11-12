# ruff: noqa: S101
"""W3: who is speaking. Speaker identity, gender, speaker-count and dossier aliases."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from src.publication.article_context import (
    ArticleEditorialContext,
    ArticleSupport,
    PublicationWindow,
)
from src.publication.article_coverage import ArticleCoveragePlan, ArticleStoryCoverage
from src.publication.article_editor import _trim_unsupported_sentences
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_quality import ArticleReaderQualityFinding
from src.publication.article_speaker import (
    ARTICLE_SPEAKER_WRITER_RULE,
    article_speaker_editor_instruction,
    detect_speaker_gender,
    speaker_key_for,
    speaker_quality_findings,
    speaker_validation_issues,
)
from src.publication.article_validator import ArticleValidationResult
from src.publication.article_writer_context import (
    _compact_evidence_record,
    render_article_writer_context_with_stats,
)

KEY_A = "a" * 24
KEY_B = "b" * 24
KEY_C = "c" * 24
KEY_D = "d" * 24

# Real texts from the production run; author names and Telegram ids are never fixtures.
TEXT_274_FIRST = (
    "Кому свет нужен? На черемушках напряжение 274 вольта, могу могу 100 вольт отсыпать "
    "кому надо (приходите со своей тарой)"
)
TEXT_274_REPLY = (
    "А кто хвастается? На ПК видеокарта сгорела и блок питания монитора. Было бы чем хвастаться"
)
TEXT_YUGM_ASK = "Давно ?"
TEXT_YUGM_FIRST = "Югм дали свет на 5 минут и выключили 👍 Спасибо РЭС"
TEXT_YUGM_ANSWER = "Да 5 минут назад дали и уже выключили"
TEXT_MOY_ENERGO = (
    "Через приложение Мой Энергосбыт делаете заявку .Там есть Нет света , нажимаете и "
    "описываете вашу ситуацию"
)
TEXT_DISPATCHER = (
    "И в официальных каналах писали что перефрезка водоводов В1 коса Слободка колония собачья "
    "балка без воды до окончания ремонтных работ. Как мне диспетчер сказала это может длиться "
    "и сутки и двое и трое"
)
TEXT_THIRD_DAY = "Сегодня на третий день дозвонилась. Смысла не вижу туда звонить"


def _support(
    support_id: str,
    *,
    text: str,
    source: str,
    key: str,
    reply_key: str = "",
    story_id: str = "story:1",
    publication_use: str = "PUBLISH",
    evidence_kind: str = "community_report",
) -> ArticleSupport:
    return ArticleSupport(
        support_id=support_id,
        text=text,
        source_text=source,
        support_kind="evidence",
        publication_use=publication_use,  # type: ignore[arg-type]
        source_refs=(f"ref:{support_id}",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=dt.datetime(2026, 11, 11, 18, 53, tzinfo=dt.timezone.utc),
        evidence_kind=evidence_kind,
        source_roles=("community",),
        story_id=story_id,
        speaker_key=key,
        reply_parent_speaker_key=reply_key,
    )


def _context(*supports: ArticleSupport) -> SimpleNamespace:
    return SimpleNamespace(
        support_by_id={support.support_id: support for support in supports},
        support_index=tuple(supports),
    )


def _draft(*paragraphs: ArticleParagraph) -> StructuredArticleDraft:
    return StructuredArticleDraft(
        title="Свет и вода",
        title_support_ids=(),
        lead="Вводная часть.",
        lead_support_ids=(),
        sections=(ArticleSection(heading="Свет", paragraphs=paragraphs),),
    )


def _paragraph(text: str, claims: tuple[tuple[str, tuple[str, ...]], ...]) -> ArticleParagraph:
    atoms = tuple(ArticleClaimAtom(text=claim, cited_support_ids=ids) for claim, ids in claims)
    cited = tuple(dict.fromkeys(sid for _, ids in claims for sid in ids))
    return ArticleParagraph(text=text, cited_support_ids=cited, claims=atoms)


# ---- W3.1 / W3.2: identity keys and aliases -------------------------------------------


def test_same_platform_author_in_one_source_gets_one_key():
    assert speaker_key_for(7, "918273645", "Анна") == speaker_key_for(7, "918273645", "Другое имя")


def test_display_name_is_a_fallback_identity_only_within_its_source():
    assert speaker_key_for(7, None, "Анна") == speaker_key_for(7, None, "Анна")
    assert speaker_key_for(7, None, "Анна") != speaker_key_for(8, None, "Анна")
    assert speaker_key_for(7, "1", "Анна") != speaker_key_for(7, None, "Анна")


def test_placeholder_or_missing_name_is_not_an_identity():
    assert speaker_key_for(7, None, "Unknown") == ""
    assert speaker_key_for(7, None, None) == ""
    assert speaker_key_for(7, "", "") == ""


def test_dossier_gives_one_alias_per_person_and_no_identity_leaks():
    own_id = "918273645"
    first = _support(
        "story:1:evidence:0:frag:1", text=TEXT_274_FIRST, source=TEXT_274_FIRST, key=KEY_A
    )
    reply = _support(
        "story:1:evidence:1:frag:2",
        text="На ПК сгорела видеокарта.",
        source=TEXT_274_REPLY,
        key=KEY_A,
        reply_key=KEY_A,
    )
    other = _support(
        "story:1:evidence:2:frag:3",
        text="В ЮГМ свет дали на 5 минут.",
        source=TEXT_YUGM_FIRST,
        key=KEY_B,
    )
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=(first, reply, other),
        support_by_id={s.support_id: s for s in (first, reply, other)},
        recurring_topics=(),
        publication_window=PublicationWindow(
            snapshot_at=dt.datetime(2026, 11, 12, 18, 0, tzinfo=dt.timezone.utc),
            lookback_start=dt.datetime(2026, 11, 11, 18, 0, tzinfo=dt.timezone.utc),
        ),
    )
    coverage = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Свет",
                rank=1,
                prominence="DEVELOP",
                support_ids=(first.support_id, reply.support_id, other.support_id),
            ),
        )
    )

    rendered, _stats = render_article_writer_context_with_stats(context, coverage)

    assert '"speaker":"S1"' in rendered
    assert '"speaker":"S2"' in rendered
    assert '"reply_to_speaker":"S1"' in rendered
    assert "S1" in rendered and rendered.count('"speaker":"S1"') == 2
    for leaked in (KEY_A, KEY_B, own_id, "Анна", "@"):
        assert leaked not in rendered


def test_equal_wording_from_two_speakers_stays_two_records():
    text = "Свет дали на 5 минут."
    first = _support("story:1:evidence:0:frag:1", text=text, source=text, key=KEY_A)
    second = _support("story:1:evidence:1:frag:2", text=text, source=text, key=KEY_B)
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=(first, second),
        support_by_id={first.support_id: first, second.support_id: second},
        recurring_topics=(),
    )
    coverage = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Свет",
                rank=1,
                prominence="DEVELOP",
                support_ids=(first.support_id, second.support_id),
            ),
        )
    )

    rendered, _stats = render_article_writer_context_with_stats(context, coverage)

    assert rendered.count('"speaker":"S1"') == 1
    assert rendered.count('"speaker":"S2"') == 1


def test_compact_encoding_keeps_speaker_alias_and_gender():
    record: dict[str, object] = {
        "fact": "Свет дали на 5 минут.",
        "story_id": "story:1",
        "support_kind": "evidence",
        "evidence_kind": "community_report",
        "source_roles": ["community"],
        "framing": "attributed_report",
        "temporal_role": "CURRENT_WINDOW",
        "navigation": [],
        "support_ids": ["story:1:evidence:0:frag:1"],
        "provenance_by_support_id": [
            {
                "support_id": "story:1:evidence:0:frag:1",
                "source_refs": [],
                "fragment_ids": [1],
                "source_item_ids": [1],
            }
        ],
        "speaker": "S1",
        "reply_to_speaker": "S2",
        "speaker_gender": "f",
    }

    compact = _compact_evidence_record(record)

    assert compact["sp"] == "S1"
    assert compact["rs"] == "S2"
    assert compact["sg"] == "f"


# ---- W3.4: gender from the speaker's own text ------------------------------------------


def test_own_past_tense_self_description_marks_a_woman():
    assert detect_speaker_gender(TEXT_THIRD_DAY) == "f"
    assert detect_speaker_gender("Я звонил в ЖЭК, никто не ответил") == "m"


def test_third_person_subject_stays_unknown():
    assert detect_speaker_gender(TEXT_DISPATCHER) == "unknown"
    assert detect_speaker_gender("Диспетчер сказала, что завтра") == "unknown"


def test_noun_that_looks_like_a_verb_does_not_mark_gender():
    assert detect_speaker_gender("Смысла не вижу туда звонить") == "unknown"


def test_conflicting_self_descriptions_are_unknown():
    assert detect_speaker_gender("Я была дома. Я звонил в ЖЭК") == "unknown"


def test_writer_rule_forbids_alias_leaks_and_other_person_wording():
    assert "другой житель" in ARTICLE_SPEAKER_WRITER_RULE
    assert "S1" in ARTICLE_SPEAKER_WRITER_RULE
    assert "speaker_gender" in ARTICLE_SPEAKER_WRITER_RULE


# ---- W3.3: SPEAKER_MULTIPLICITY_MISMATCH (blocking) --------------------------------------


def test_another_resident_with_the_same_author_is_blocked():
    first = _support("s1", text=TEXT_274_FIRST, source=TEXT_274_FIRST, key=KEY_A)
    second = _support("s2", text="На ПК сгорела видеокарта.", source=TEXT_274_REPLY, key=KEY_A)
    draft = _draft(
        _paragraph(
            "На Черемушках житель сообщил о напряжении 274 вольта в сети. "
            "У другого горожанина, по его сообщению, на ПК сгорели видеокарта и блок питания монитора.",
            (
                ("На Черемушках житель сообщил о напряжении 274 вольта в сети.", ("s1",)),
                (
                    "У другого горожанина, по его сообщению, на ПК сгорели видеокарта и блок питания монитора.",
                    ("s2",),
                ),
            ),
        )
    )

    issues = speaker_validation_issues(draft, _context(first, second))

    assert [issue.code for issue in issues] == ["SPEAKER_MULTIPLICITY_MISMATCH"]
    issue = issues[0]
    assert issue.unit_id == "P001"
    assert issue.blocking is True
    assert issue.claim_text and issue.claim_text.startswith("У другого горожанина")


def test_another_resident_with_a_different_author_passes():
    first = _support("s1", text=TEXT_274_FIRST, source=TEXT_274_FIRST, key=KEY_A)
    second = _support("s2", text="На ПК сгорела видеокарта.", source=TEXT_274_REPLY, key=KEY_B)
    draft = _draft(
        _paragraph(
            "На Черемушках житель сообщил о напряжении 274 вольта в сети. "
            "У другого горожанина на ПК сгорели видеокарта и блок питания.",
            (
                ("На Черемушках житель сообщил о напряжении 274 вольта в сети.", ("s1",)),
                ("У другого горожанина на ПК сгорели видеокарта и блок питания.", ("s2",)),
            ),
        )
    )

    assert speaker_validation_issues(draft, _context(first, second)) == []


def test_same_sentence_yugm_claim_from_one_author_is_blocked():
    first = _support(
        "s3", text="В ЮГМ свет дали на 5 минут и выключили.", source=TEXT_YUGM_FIRST, key=KEY_A
    )
    second = _support(
        "s4", text="Подачу дали около пяти минут назад.", source=TEXT_YUGM_ANSWER, key=KEY_A
    )
    sentence = (
        "В ЮГМ свет дали на 5 минут и выключили, а другой житель сообщил, что подачу дали "
        "около пяти минут назад и уже отключили."
    )
    draft = _draft(_paragraph(sentence, ((sentence, ("s3", "s4")),)))

    issues = speaker_validation_issues(draft, _context(first, second))

    assert [issue.code for issue in issues] == ["SPEAKER_MULTIPLICITY_MISMATCH"]


def test_same_sentence_yugm_claim_with_two_authors_is_not_evaluated():
    first = _support(
        "s3", text="В ЮГМ свет дали на 5 минут и выключили.", source=TEXT_YUGM_FIRST, key=KEY_A
    )
    second = _support(
        "s4", text="Подачу дали около пяти минут назад.", source=TEXT_YUGM_ANSWER, key=KEY_B
    )
    sentence = (
        "В ЮГМ свет дали на 5 минут и выключили, а другой житель сообщил, что подачу дали "
        "около пяти минут назад и уже отключили."
    )
    draft = _draft(_paragraph(sentence, ((sentence, ("s3", "s4")),)))

    assert speaker_validation_issues(draft, _context(first, second)) == []


def test_unknown_speaker_identity_is_not_evaluated():
    first = _support("s1", text=TEXT_274_FIRST, source=TEXT_274_FIRST, key=KEY_A)
    second = _support("s2", text="На ПК сгорела видеокарта.", source=TEXT_274_REPLY, key="")
    draft = _draft(
        _paragraph(
            "Житель сообщил о напряжении. Другой житель сообщил о сгоревшей видеокарте.",
            (
                ("Житель сообщил о напряжении.", ("s1",)),
                ("Другой житель сообщил о сгоревшей видеокарте.", ("s2",)),
            ),
        )
    )

    assert speaker_validation_issues(draft, _context(first, second)) == []


def test_blocking_finding_is_trimmed_by_the_existing_sentence_backstop():
    first = _support("s1", text=TEXT_274_FIRST, source=TEXT_274_FIRST, key=KEY_A)
    second = _support("s2", text="На ПК сгорела видеокарта.", source=TEXT_274_REPLY, key=KEY_A)
    third = _support("s3", text="Напряжение 274 вольта.", source=TEXT_274_FIRST, key=KEY_A)
    draft = _draft(
        _paragraph(
            "Житель сообщил о напряжении 274 вольта. У другого горожанина сгорела видеокарта. "
            "Напряжение 274 вольта держалось весь вечер.",
            (
                ("Житель сообщил о напряжении 274 вольта.", ("s1",)),
                ("У другого горожанина сгорела видеокарта.", ("s2",)),
                ("Напряжение 274 вольта держалось весь вечер.", ("s3",)),
            ),
        )
    )
    issues = speaker_validation_issues(draft, _context(first, second, third))
    validation = ArticleValidationResult(
        is_valid=False, word_count=0, section_count=1, issues=tuple(issues)
    )

    trimmed = _trim_unsupported_sentences(draft, validation)

    assert trimmed is not None
    new_draft, counts = trimmed
    assert counts == {"P001": 1}
    text = new_draft.sections[0].paragraphs[0].text
    assert "другой горожанин" not in text
    assert "Житель сообщил о напряжении 274 вольта." in text
    assert "держалось весь вечер" in text


def test_editor_instruction_exists_for_speaker_codes():
    assert "другой житель" in (
        article_speaker_editor_instruction("SPEAKER_MULTIPLICITY_MISMATCH") or ""
    )
    assert article_speaker_editor_instruction("PLURAL_SOURCE_INFLATION")
    assert article_speaker_editor_instruction("UNSUPPORTED_SPEAKER_GENDER")


# ---- W3.3: PLURAL_SOURCE_INFLATION and W3.4: UNSUPPORTED_SPEAKER_GENDER (repair) --------


def _codes(findings: list[ArticleReaderQualityFinding]) -> list[str]:
    return [finding.code for finding in findings]


def test_residents_advise_from_one_message_is_flagged_as_inflation():
    support = _support("s5", text=TEXT_MOY_ENERGO, source=TEXT_MOY_ENERGO, key=KEY_A)
    draft = _draft(
        _paragraph(
            "Жители советуют подавать заявку через приложение «Мой Энергосбыт».",
            (("Жители советуют подавать заявку через приложение «Мой Энергосбыт».", ("s5",)),),
        )
    )

    findings = speaker_quality_findings(draft, _context(support))

    assert _codes(findings) == ["PLURAL_SOURCE_INFLATION"]
    assert findings[0].unit_id == "P001"
    assert findings[0].severity == "repair"


def test_one_resident_advises_is_not_flagged():
    support = _support("s5", text=TEXT_MOY_ENERGO, source=TEXT_MOY_ENERGO, key=KEY_A)
    draft = _draft(
        _paragraph(
            "Один из жителей советует подавать заявку через приложение «Мой Энергосбыт».",
            (
                (
                    "Один из жителей советует подавать заявку через приложение «Мой Энергосбыт».",
                    ("s5",),
                ),
            ),
        )
    )

    assert speaker_quality_findings(draft, _context(support)) == []


def test_plural_residents_with_two_authors_are_not_flagged():
    first = _support("s5", text=TEXT_MOY_ENERGO, source=TEXT_MOY_ENERGO, key=KEY_A)
    second = _support("s6", text="Нет света.", source=TEXT_YUGM_ASK, key=KEY_B)
    draft = _draft(
        _paragraph(
            "Жители советуют подавать заявку через приложение «Мой Энергосбыт».",
            (("Жители советуют подавать заявку через приложение «Мой Энергосбыт».", ("s5", "s6")),),
        )
    )

    assert speaker_quality_findings(draft, _context(first, second)) == []


def test_female_noun_without_female_speaker_is_flagged():
    support = _support("s7", text=TEXT_DISPATCHER, source=TEXT_DISPATCHER, key=KEY_C)
    draft = _draft(
        _paragraph(
            "Жительница сообщила, что воды не будет до окончания ремонта.",
            (("Жительница сообщила, что воды не будет до окончания ремонта.", ("s7",)),),
        )
    )

    findings = speaker_quality_findings(draft, _context(support))

    assert _codes(findings) == ["UNSUPPORTED_SPEAKER_GENDER"]


def test_female_noun_with_own_female_self_description_passes():
    support = _support("s8", text=TEXT_THIRD_DAY, source=TEXT_THIRD_DAY, key=KEY_D)
    draft = _draft(
        _paragraph(
            "Жительница сообщила, что дозвонилась до диспетчерской на третий день.",
            (("Жительница сообщила, что дозвонилась до диспетчерской на третий день.", ("s8",)),),
        )
    )

    assert speaker_quality_findings(draft, _context(support)) == []
