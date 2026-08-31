"""Unit tests for non-blocking prose quality diagnostics."""

from __future__ import annotations

import datetime as dt

from src.publication.digest_narrative import (
    DigestEditorialItemDraft,
    DigestNarrativeBlockDraft,
    DigestNarrativeDraft,
)
from src.publication.digest_quality_diagnostics import (
    DIGEST_DIAGNOSTICS_VERSION,
    audit_digest_prose_quality,
)
from src.publication.evidence import PublicationEvidence


def _make_evidence(
    eid: str,
    sid: int,
    text: str,
    kind: str = "established_fact",
    pub_use: str = "PUBLISH",
) -> PublicationEvidence:
    now = dt.datetime(2026, 8, 29, 12, 0, tzinfo=dt.timezone.utc)
    return PublicationEvidence(
        evidence_id=eid,
        story_id=sid,
        text=text,
        source_text=text,
        kind=kind,
        publication_use=pub_use,
        fragment_id=1,
        source_ref="ref:1",
        source_id=1,
        source_item_id=1,
        source_role="citizen",
        observed_at=now,
    )


def test_clean_draft_returns_no_warnings():
    evi = _make_evidence("evi:1", 1, "Автобус №4 возобновил движение")
    evidence = {"evi:1": evi}

    item = DigestEditorialItemDraft(
        headline="Возобновлено движение автобуса №4",
        body="По сообщениям очевидцев, транспорт ходит с интервалом в 30 минут по обычному маршруту.",
        covered_story_ids=("story:1",),
        cited_support_ids=("evi:1",),
    )
    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:trans",
                items=(item,),
            ),
        )
    )

    audit = audit_digest_prose_quality(draft, evidence)
    assert audit.version == DIGEST_DIAGNOSTICS_VERSION
    assert audit.is_clean is True
    assert len(audit.warnings) == 0


def test_detects_duplicated_attribution():
    evi = _make_evidence("evi:1", 1, "На Горе нет света")
    evidence = {"evi:1": evi}

    item = DigestEditorialItemDraft(
        headline="Жители сообщают об отключении света на Горе",
        body="По сообщениям жителей, электричество пропало около полудня во всем микрорайоне.",
        covered_story_ids=("story:1",),
        cited_support_ids=("evi:1",),
    )
    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:util",
                items=(item,),
            ),
        )
    )

    audit = audit_digest_prose_quality(draft, evidence)
    assert audit.is_clean is False
    codes = [w.code for w in audit.warnings]
    assert "DUPLICATED_ATTRIBUTION" in codes
    warning = next(w for w in audit.warnings if w.code == "DUPLICATED_ATTRIBUTION")
    assert warning.block_id == "block:util"
    assert warning.item_index == 0


def test_detects_question_as_meta_news():
    evi = _make_evidence(
        "evi:1",
        1,
        "Работает ли пенсионный фонд?",
        kind="resident_question",
        pub_use="CONTEXT",
    )

    evidence = {"evi:1": evi}

    item = DigestEditorialItemDraft(
        headline="Жители интересуются работой пенсионного фонда",
        body="В соцсетях появились вопросы о графике приема граждан в понедельник.",
        covered_story_ids=("story:1",),
        cited_support_ids=("evi:1",),
    )
    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:soc",
                items=(item,),
            ),
        )
    )

    audit = audit_digest_prose_quality(draft, evidence)
    codes = [w.code for w in audit.warnings]
    assert "QUESTION_AS_META_NEWS" in codes


def test_detects_redundant_headline_in_body():
    evi = _make_evidence("evi:1", 1, "Спорткомплекс открыл набор")
    evidence = {"evi:1": evi}

    item = DigestEditorialItemDraft(
        headline="Спорткомплекс открыл бесплатный набор детей",
        body="Спорткомплекс открыл бесплатный набор детей. Занятия начнутся со следующей недели.",
        covered_story_ids=("story:1",),
        cited_support_ids=("evi:1",),
    )
    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:soc",
                items=(item,),
            ),
        )
    )

    audit = audit_digest_prose_quality(draft, evidence)
    codes = [w.code for w in audit.warnings]
    assert "REDUNDANT_HEADLINE_IN_BODY" in codes
