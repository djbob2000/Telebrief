# ruff: noqa: S101
from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from test_digest_source_material import _evidence

from src.publication import digest_reporting_context

SNAPSHOT = dt.datetime(2026, 10, 6, 13, tzinfo=dt.timezone.utc)


def mark(items, *, hours=24):
    annotate = getattr(digest_reporting_context, "annotate_digest_reporting_window", None)
    assert callable(annotate), "Digest needs source window roles independent of event dates"
    return annotate(items, snapshot_at=SNAPSHOT, lookback_hours=hours)


def test_old_source_is_preserved_as_historical_without_new_event_date():
    source = replace(
        _evidence(1, "На Павлова сейчас нет света.", "На Павлова сейчас нет света."),
        observed_at=SNAPSHOT - dt.timedelta(days=6),
    )
    annotated = mark({source.evidence_id: source})
    result = annotated[source.evidence_id]
    assert result.text == source.text
    assert result.source_text == source.source_text
    assert result.publication_use == "PUBLISH"
    assert result.reporting_window_role == "historical_source"
    metadata = digest_reporting_context.publish_support_metadata(annotated)[source.source_ref]
    assert metadata["reporting_window_role"] == "historical_source"
    assert "observed_at" not in metadata


def test_current_window_source_does_not_prove_persistence_until_publication():
    source = replace(
        _evidence(1, "На Павлова нет света.", "На Павлова нет света."),
        observed_at=SNAPSHOT - dt.timedelta(hours=2),
    )
    result = mark({source.evidence_id: source})[source.evidence_id]
    assert result.reporting_window_role == "current_window_source"
    assert result.text == source.text


def test_frozen_window_length_is_used_instead_of_fixed_one_day():
    source = replace(
        _evidence(1, "На Павлова нет света.", "На Павлова нет света."),
        observed_at=SNAPSHOT - dt.timedelta(hours=30),
    )
    assert (
        mark({source.evidence_id: source})[source.evidence_id].reporting_window_role
        == "historical_source"
    )
    assert (
        mark({source.evidence_id: source}, hours=48)[source.evidence_id].reporting_window_role
        == "current_window_source"
    )


def _grace_evidence(role: str):
    import datetime as dt

    from src.publication.evidence import PublicationEvidence

    text = "По сообщению жителя Бердянска, возле Грации свет отключили вчера в 12, а сегодня уже включили."
    return PublicationEvidence(
        evidence_id="ev:grace",
        story_id=37604,
        text=text,
        source_text="Возле Грации вчера в 12 выключили, сегодня уже дали",
        kind="community_report",
        publication_use="PUBLISH",
        fragment_id=102277,
        source_ref="ev:grace",
        source_id=1,
        source_item_id=1,
        source_role="community",
        observed_at=dt.datetime(2026, 10, 4, 5, 58, tzinfo=dt.UTC),
        reporting_window_role=role,
    )


def _grace_draft(body: str):
    from src.publication.digest_narrative import (
        DigestEditorialItemDraft,
        DigestNarrativeBlockDraft,
        DigestNarrativeDraft,
    )

    item = DigestEditorialItemDraft(item_id="item:grace", body=body, cited_support_ids=("ev:grace",))
    return DigestNarrativeDraft(blocks=(DigestNarrativeBlockDraft(block_id="b", items=(item,)),))


def test_relative_day_from_pre_window_report_requests_repair_without_blocking():
    """Run 309: a 04.10 report's «вчера/сегодня» was dated to the 05.10 digest."""
    from src.publication.digest_quality_diagnostics import audit_digest_prose_quality

    body = "Возле Грации, по более раннему сообщению жителя, свет отключили вчера в 12, а сегодня уже включили."
    audit = audit_digest_prose_quality(_grace_draft(body), {"ev:grace": _grace_evidence("historical_source")})
    assert "HISTORICAL_RELATIVE_DAY" in {w.code for w in audit.warnings}
    assert audit.is_publishable


@pytest.mark.parametrize(
    ("role", "body"),
    [
        (
            "current_window_source",
            "Возле Грации свет отключили вчера в 12, а сегодня уже включили.",
        ),
        (
            "historical_source",
            "Возле «Грации», по более раннему сообщению жителя, свет отключили накануне в 12, "
            "а на следующий день включили.",
        ),
    ],
)
def test_relative_day_is_allowed_for_current_or_rephrased_reports(role, body):
    from src.publication.digest_quality_diagnostics import audit_digest_prose_quality

    audit = audit_digest_prose_quality(_grace_draft(body), {"ev:grace": _grace_evidence(role)})
    assert "HISTORICAL_RELATIVE_DAY" not in {w.code for w in audit.warnings}
