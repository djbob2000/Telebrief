from __future__ import annotations

import datetime as dt

from src.config_loader import PublicationEditorialConfig
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_coverage import (
    ArticleCoveragePlan,
    ArticleStoryCoverage,
)
from src.publication.article_coverage_diagnostics import (
    diagnose_article_coverage,
)
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_validator import validate_article_draft


def _make_dummy_draft(
    cited_ids: tuple[str, ...],
    text_content: str = "Обычный текст статьи без контактов.",
) -> StructuredArticleDraft:
    p_claims = (
        (ArticleClaimAtom(text=text_content, cited_support_ids=cited_ids),) if cited_ids else ()
    )
    p = ArticleParagraph(text=text_content, cited_support_ids=cited_ids, claims=p_claims)
    sec = ArticleSection(
        heading="Раздел",
        heading_support_ids=cited_ids[:1],
        paragraphs=(p,),
    )
    words = len(f"Заголовок Лид статьи Раздел {text_content}".split())
    t_claims = (
        (ArticleClaimAtom(text=text_content, cited_support_ids=cited_ids[:1]),) if cited_ids else ()
    )
    l_claims = (
        (ArticleClaimAtom(text=text_content, cited_support_ids=cited_ids[:1]),) if cited_ids else ()
    )
    return StructuredArticleDraft(
        title=text_content,
        title_support_ids=cited_ids[:1],
        title_claims=t_claims,
        lead=text_content,
        lead_support_ids=cited_ids[:1],
        lead_claims=l_claims,
        sections=(sec,),
        word_count=words,
    )


def test_diagnose_article_coverage_prominence_and_detail():
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:power",
                topic="Свет",
                rank=1,
                prominence="DEVELOP",
                support_ids=("story:power:1", "story:power:2"),
                detail_support_ids=("story:power:1", "story:power:2"),
            ),
            ArticleStoryCoverage(
                story_id="story:telecom",
                topic="Связь",
                rank=2,
                prominence="WEAVE",
                support_ids=("story:telecom:1", "story:telecom:2"),
                detail_support_ids=("story:telecom:1",),
            ),
            ArticleStoryCoverage(
                story_id="story:sport",
                topic="Спорт",
                rank=3,
                prominence="BRIEF",
                support_ids=("story:sport:1",),
                detail_support_ids=("story:sport:1",),
            ),
        )
    )

    # Draft cites power:1 (DEVELOP) and sport:1 (BRIEF); telecom (WEAVE) is omitted
    draft = _make_dummy_draft(cited_ids=("story:power:1", "story:sport:1"))

    diag = diagnose_article_coverage(draft, plan)
    assert diag.planned_story_count == 3
    assert diag.covered_story_count == 2
    assert diag.uncovered_story_ids == ("story:telecom",)
    assert diag.develop_story_coverage == 1.0
    assert diag.weave_story_coverage == 0.0
    assert diag.brief_story_coverage == 1.0

    # Detail supports: planned = power:1, power:2, telecom:1, sport:1 (4 total)
    # covered = power:1, sport:1 (2 total)
    assert diag.planned_detail_support_count == 4
    assert diag.covered_detail_support_count == 2
    assert diag.detail_support_coverage == 0.5
    assert set(diag.uncovered_detail_support_ids) == {"story:power:2", "story:telecom:1"}


def test_diagnose_article_coverage_detects_contact_leaks():
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:route",
                topic="Транспорт",
                rank=1,
                prominence="BRIEF",
                support_ids=("story:route:1",),
            ),
        )
    )
    draft = _make_dummy_draft(
        cited_ids=("story:route:1",),
        text_content="Автобус ходит каждый день. Звоните +79901234567 или переходите на https://example.com",
    )
    diag = diagnose_article_coverage(draft, plan)
    assert len(diag.leaked_contact_payloads) >= 2
    assert any("+79901234567" in leak for leak in diag.leaked_contact_payloads)
    assert any("https://example.com" in leak for leak in diag.leaked_contact_payloads)


def test_diagnostics_are_non_blocking_on_validation():
    now = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
    s1 = ArticleSupport(
        support_id="story:power:evidence:0:frag:1",
        text="Света нет",
        source_text="Света нет",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=now,
        story_id="story:power",
    )
    s2 = ArticleSupport(
        support_id="story:power:evidence:1:frag:2",
        text="Генератор 300 рублей",
        source_text="Генератор 300 рублей",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-2",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=now,
        story_id="story:power",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Свет",),
        support_index=(s1, s2),
        support_by_id={s.support_id: s for s in (s1, s2)},
        recurring_topics=(),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:power",
                topic="Свет",
                rank=1,
                prominence="DEVELOP",
                support_ids=(s1.support_id, s2.support_id),
                detail_support_ids=(s1.support_id, s2.support_id),
            ),
        )
    )
    # Draft only cites s1, omitting detail s2
    draft = _make_dummy_draft(
        cited_ids=(s1.support_id,),
        text_content="Света нет",
    )

    validation = validate_article_draft(
        draft,
        context,
        PublicationEditorialConfig(article_min_sections=1, article_min_words=5),
    )
    assert validation.is_valid is True

    diag = diagnose_article_coverage(draft, plan)
    assert diag.detail_support_coverage < 1.0


def test_diagnose_article_coverage_explicit_story_coverage():
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Story 1",
                rank=1,
                prominence="DEVELOP",
                support_ids=("story:1:1",),
                detail_support_ids=("story:1:1",),
            ),
            ArticleStoryCoverage(
                story_id="story:2",
                topic="Story 2",
                rank=2,
                prominence="DEVELOP",
                support_ids=("story:2:1",),
                detail_support_ids=("story:2:1",),
            ),
        )
    )
    draft = _make_dummy_draft(cited_ids=("story:1:1",))
    diag = diagnose_article_coverage(draft, plan)
    assert diag.covered_story_ids == ("story:1",)
    assert diag.uncovered_story_ids == ("story:2",)
    assert diag.story_coverage == 0.5


def test_diagnose_article_coverage_adversarial_paragraph_overcitation():
    # Test A: Paragraph cites all 17 supports, but its claim only cites/supports Story 1.
    # Story coverage must be 1/17, NOT 17/17!
    stories = []
    all_17_supports = []
    for i in range(1, 18):
        sid = f"story:{i}:1"
        all_17_supports.append(sid)
        stories.append(
            ArticleStoryCoverage(
                story_id=f"story:{i}",
                topic=f"Topic {i}",
                rank=i,
                prominence="DEVELOP" if i == 1 else "WEAVE",
                support_ids=(sid,),
                detail_support_ids=(sid,),
            )
        )
    plan = ArticleCoveragePlan(stories=tuple(stories))

    # Paragraph cited_support_ids has ALL 17 supports!
    # But claim only cites support 1!
    claim1 = ArticleClaimAtom(
        text="Текст только про первую историю",
        cited_support_ids=("story:1:1",),
    )
    para = ArticleParagraph(
        text="Текст только про первую историю",
        cited_support_ids=tuple(all_17_supports),
        claims=(claim1,),
    )
    sec = ArticleSection(
        heading="Раздел",
        heading_support_ids=("story:1:1",),
        paragraphs=(para,),
    )
    draft = StructuredArticleDraft(
        title="Заголовок статьи",
        title_support_ids=("story:1:1",),
        title_claims=(),
        lead="Лид статьи",
        lead_support_ids=("story:1:1",),
        lead_claims=(),
        sections=(sec,),
        word_count=50,
    )

    diag = diagnose_article_coverage(draft, plan)
    assert diag.covered_story_count == 1
    assert diag.covered_story_ids == ("story:1",)
    assert len(diag.uncovered_story_ids) == 16
    assert abs(diag.story_coverage - (1 / 17)) < 1e-6


def test_diagnose_article_coverage_adversarial_title_lead_provenance_repair_does_not_credit_omitted_story():
    # Test B: Raw writer omits Story 1 DEVELOP, writing only about Story 2.
    # Even if title/lead provenance repair inserts Story 1 support into title/lead,
    # Story 1 remains uncovered unless a validated claim covers it!
    now = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
    s1_sup = ArticleSupport(
        support_id="story:1:1",
        text="Света нет на Горе уже сутки",
        source_text="Света нет на Горе уже сутки",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=now,
        story_id="story:1",
    )
    s2_sup = ArticleSupport(
        support_id="story:2:1",
        text="Автобусы в Бердянске ходят по обычному расписанию",
        source_text="Автобусы в Бердянске ходят по обычному расписанию",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-2",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=now,
        story_id="story:2",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Городские новости",),
        support_index=(s1_sup, s2_sup),
        support_by_id={s1_sup.support_id: s1_sup, s2_sup.support_id: s2_sup},
        recurring_topics=(),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Свет",
                rank=1,
                prominence="DEVELOP",
                support_ids=("story:1:1",),
                detail_support_ids=("story:1:1",),
            ),
            ArticleStoryCoverage(
                story_id="story:2",
                topic="Транспорт",
                rank=2,
                prominence="WEAVE",
                support_ids=("story:2:1",),
                detail_support_ids=("story:2:1",),
            ),
        )
    )

    # Draft where title/lead wrapper has Story 1 support,
    # but the text is only about transport (Story 2)
    p2_claim = ArticleClaimAtom(
        text="Автобусы в Бердянске ходят по обычному расписанию",
        cited_support_ids=("story:2:1",),
    )
    p2 = ArticleParagraph(
        text="Автобусы в Бердянске ходят по обычному расписанию",
        cited_support_ids=("story:2:1",),
        claims=(p2_claim,),
    )
    sec2 = ArticleSection(
        heading="Транспорт",
        heading_support_ids=("story:2:1",),
        paragraphs=(p2,),
    )
    lead_claim_transport = ArticleClaimAtom(
        text="Городской транспорт продолжает работу в штатном режиме.",
        cited_support_ids=("story:1:1",),  # inserted by flawed provenance repair!
    )
    draft = StructuredArticleDraft(
        title="Новости транспорта Бердянска",
        title_support_ids=("story:1:1",),  # inserted by title provenance repair!
        title_claims=(),
        lead="Городской транспорт продолжает работу в штатном режиме.",
        lead_support_ids=("story:1:1",),  # inserted by lead provenance repair!
        lead_claims=(lead_claim_transport,),
        sections=(sec2,),
        word_count=40,
    )

    diag = diagnose_article_coverage(draft, plan, context=context)
    # Story 1 DEVELOP was omitted by writer and not validated by any claim:
    assert "story:1" in diag.uncovered_story_ids
    assert diag.develop_story_coverage == 0.0
    assert "story:2" in diag.covered_story_ids
    assert diag.covered_story_count == 1
    assert diag.story_coverage == 0.5


def test_diagnose_article_coverage_adversarial_claim_omitted_from_draft():
    # Test 1: Story 1 and Story 2 are planned.
    # Paragraph text cites Story 1 & 2 wrapper IDs, but claims list contains ONLY a claim for Story 1.
    # Story 2 must NOT receive coverage credit.
    now = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
    s1 = ArticleSupport(
        support_id="story:1:1",
        text="Электроэнергия отсутствует в центре города",
        source_text="Электроэнергия отсутствует в центре города",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=now,
        story_id="story:1",
    )
    s2 = ArticleSupport(
        support_id="story:2:1",
        text="Водоснабжение отключено на Восточном проспекте",
        source_text="Водоснабжение отключено на Восточном проспекте",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-2",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=now,
        story_id="story:2",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Новости",),
        support_index=(s1, s2),
        support_by_id={s1.support_id: s1, s2.support_id: s2},
        recurring_topics=(),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Свет",
                rank=1,
                prominence="DEVELOP",
                support_ids=("story:1:1",),
                detail_support_ids=("story:1:1",),
            ),
            ArticleStoryCoverage(
                story_id="story:2",
                topic="Вода",
                rank=2,
                prominence="DEVELOP",
                support_ids=("story:2:1",),
                detail_support_ids=("story:2:1",),
            ),
        )
    )

    # Paragraph wrapper has both s1 and s2, but claims has ONLY s1
    claim1 = ArticleClaimAtom(
        text="Электроэнергия отсутствует в центре города.",
        cited_support_ids=("story:1:1",),
    )
    para = ArticleParagraph(
        text="Электроэнергия отсутствует в центре города.",
        cited_support_ids=("story:1:1", "story:2:1"),
        claims=(claim1,),
    )
    sec = ArticleSection(
        heading="Коммунальные службы",
        heading_support_ids=("story:1:1",),
        paragraphs=(para,),
    )
    draft = StructuredArticleDraft(
        title="Ситуация со светом и водой",
        title_support_ids=("story:1:1",),
        title_claims=(),
        lead="В городе продолжаются отключения коммунальных услуг.",
        lead_support_ids=("story:1:1",),
        lead_claims=(),
        sections=(sec,),
        word_count=35,
    )

    diag = diagnose_article_coverage(draft, plan, context=context)
    assert diag.covered_story_ids == ("story:1",)
    assert diag.uncovered_story_ids == ("story:2",)
    assert diag.story_coverage == 0.5


def test_diagnose_article_coverage_adversarial_generic_overcited_claim_rejected():
    # Test 2: Writer constructs a generic sentence ("В городе возникли сложности с подачей услуг")
    # and attaches cited_support_ids for both Story 1 (Свет) and Story 2 (Вода).
    # Neither story's discriminative anchors (e.g. "электроэнергия", "вода") are in the claim text.
    # Neither story should receive coverage credit from this ungrounded generic claim!
    now = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
    s1 = ArticleSupport(
        support_id="story:1:1",
        text="Электроэнергия и свет отключены на подстанции",
        source_text="Электроэнергия и свет отключены на подстанции",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=now,
        story_id="story:1",
    )
    s2 = ArticleSupport(
        support_id="story:2:1",
        text="Водоснабжение и подача питьевой воды приостановлены",
        source_text="Водоснабжение и подача питьевой воды приостановлены",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-2",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=now,
        story_id="story:2",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Новости",),
        support_index=(s1, s2),
        support_by_id={s1.support_id: s1, s2.support_id: s2},
        recurring_topics=(),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Свет",
                rank=1,
                prominence="DEVELOP",
                support_ids=("story:1:1",),
                detail_support_ids=("story:1:1",),
            ),
            ArticleStoryCoverage(
                story_id="story:2",
                topic="Вода",
                rank=2,
                prominence="DEVELOP",
                support_ids=("story:2:1",),
                detail_support_ids=("story:2:1",),
            ),
        )
    )

    # Generic claim over-citing both stories without their discriminative anchors
    generic_claim = ArticleClaimAtom(
        text="В различных районах города наблюдаются определенные сложности.",
        cited_support_ids=("story:1:1", "story:2:1"),
    )
    para = ArticleParagraph(
        text="В различных районах города наблюдаются определенные сложности.",
        cited_support_ids=("story:1:1", "story:2:1"),
        claims=(generic_claim,),
    )
    sec = ArticleSection(
        heading="Городская хроника",
        heading_support_ids=("story:1:1",),
        paragraphs=(para,),
    )
    draft = StructuredArticleDraft(
        title="Сложности в городских районах",
        title_support_ids=("story:1:1",),
        title_claims=(),
        lead="Горожане сообщают о ситуации в жилых кварталах.",
        lead_support_ids=("story:1:1",),
        lead_claims=(),
        sections=(sec,),
        word_count=30,
    )

    diag = diagnose_article_coverage(draft, plan, context=context)
    # Neither story validated by generic claim!
    assert diag.covered_story_count == 0
    assert set(diag.uncovered_story_ids) == {"story:1", "story:2"}
    assert diag.story_coverage == 0.0


def test_diagnose_article_coverage_genuine_two_story_synthesis_claim_accepted():
    # Test 3: Genuine synthesized claim that explicitly mentions both stories' discriminative facts:
    # "Отключение электроэнергии нарушило работу насосов водоснабжения"
    # Both Story 1 (Свет) and Story 2 (Вода) MUST receive coverage credit!
    now = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
    s1 = ArticleSupport(
        support_id="story:1:1",
        text="Отключение электроэнергии произошло на центральной подстанции",
        source_text="Отключение электроэнергии произошло на центральной подстанции",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=now,
        story_id="story:1",
    )
    s2 = ArticleSupport(
        support_id="story:2:1",
        text="Насосы водоснабжения остановились из-за обесточивания",
        source_text="Насосы водоснабжения остановились из-за обесточивания",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-2",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=now,
        story_id="story:2",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Новости",),
        support_index=(s1, s2),
        support_by_id={s1.support_id: s1, s2.support_id: s2},
        recurring_topics=(),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Свет",
                rank=1,
                prominence="DEVELOP",
                support_ids=("story:1:1",),
                detail_support_ids=("story:1:1",),
            ),
            ArticleStoryCoverage(
                story_id="story:2",
                topic="Вода",
                rank=2,
                prominence="DEVELOP",
                support_ids=("story:2:1",),
                detail_support_ids=("story:2:1",),
            ),
        )
    )

    synthesized_claim = ArticleClaimAtom(
        text="Отключение электроэнергии на подстанции остановило насосы водоснабжения.",
        cited_support_ids=("story:1:1", "story:2:1"),
    )
    para = ArticleParagraph(
        text="Отключение электроэнергии на подстанции остановило насосы водоснабжения.",
        cited_support_ids=("story:1:1", "story:2:1"),
        claims=(synthesized_claim,),
    )
    sec = ArticleSection(
        heading="Коммунальное хозяйство",
        heading_support_ids=("story:1:1",),
        paragraphs=(para,),
    )
    draft = StructuredArticleDraft(
        title="Электроэнергия и водоснабжение",
        title_support_ids=("story:1:1",),
        title_claims=(),
        lead="В городе продолжаются аварийные работы коммунальщиков.",
        lead_support_ids=("story:1:1",),
        lead_claims=(),
        sections=(sec,),
        word_count=35,
    )

    diag = diagnose_article_coverage(draft, plan, context=context)
    assert set(diag.covered_story_ids) == {"story:1", "story:2"}
    assert diag.covered_story_count == 2
    assert diag.story_coverage == 1.0
    assert diag.develop_story_coverage == 1.0
