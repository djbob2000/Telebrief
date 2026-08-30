"""Tests for diagnostic prose quality metrics and observability."""

from __future__ import annotations

from src.publication.article_models import (
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.narrative_quality import (
    NarrativeQualityReport,
    evaluate_article_narrative,
)


def test_narrative_quality_diagnostics_on_synthesized_prose():
    # Cohesive narrative draft combining multiple supports in flowing prose
    draft = StructuredArticleDraft(
        title="Обстановка в городе",
        title_support_ids=("sup:1",),
        lead="В течение дня городские службы ликвидировали аварии на сетях.",
        lead_support_ids=("sup:1",),
        sections=(
            ArticleSection(
                heading="Энергоснабжение и водоснабжение",
                heading_support_ids=("sup:1", "sup:2"),
                paragraphs=(
                    ArticleParagraph(
                        text="Утром из-за аварии на подстанции центр города остался без света, поэтому аптеки перешли на генераторы. В то же время на Лисках водоканал возобновил подачу воды в штатном режиме.",
                        cited_support_ids=("sup:1", "sup:2"),
                    ),
                    ArticleParagraph(
                        text="По словам начальника участка, «ремонт завершился раньше графика». Жители микрорайона подтверждают стабильное давление.",
                        cited_support_ids=("sup:3", "sup:4"),
                    ),
                ),
            ),
        ),
    )

    report = evaluate_article_narrative(draft)
    assert isinstance(report, NarrativeQualityReport)
    assert report.paragraph_count == 2
    assert report.multi_support_paragraph_count == 2
    assert report.synthesis_ratio == 1.0
    assert report.database_label_patterns == 0
    assert report.direct_quote_count >= 1
    assert report.avg_sentences_per_paragraph >= 1.5


def test_narrative_quality_diagnostics_on_database_shaped_prose():
    # Database-shaped draft with label prefixes and single-support bullets
    draft = StructuredArticleDraft(
        title="Сводка событий",
        title_support_ids=("sup:1",),
        lead="Сводка за день.",
        lead_support_ids=("sup:1",),
        sections=(
            ArticleSection(
                heading="События",
                heading_support_ids=("sup:1",),
                paragraphs=(
                    ArticleParagraph(
                        text="Энергоснабжение (Центр): отключение электроэнергии.",
                        cited_support_ids=("sup:1",),
                    ),
                    ArticleParagraph(
                        text="По сообщениям жителей, нет света.",
                        cited_support_ids=("sup:1",),
                    ),
                    ArticleParagraph(
                        text="По сообщениям жителей, нет воды.",
                        cited_support_ids=("sup:2",),
                    ),
                ),
            ),
        ),
    )

    report = evaluate_article_narrative(draft)
    assert report.paragraph_count == 3
    assert report.multi_support_paragraph_count == 0
    assert report.synthesis_ratio == 0.0
    assert report.database_label_patterns >= 1
    assert report.repeated_attribution_starts >= 2
