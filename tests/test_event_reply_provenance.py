# ruff: noqa: S101

from __future__ import annotations

import datetime as dt
import inspect

from src.config_loader import load_config
from src.domain.edition_geography import resolve_edition_geography
from src.domain.event_payload import EventPayload, EvidenceItemPayload
from src.domain.event_pipeline import SourceFragment
from src.domain.service_state import ServiceStatePayload
from src.processing import event_analysis, event_triage
from src.processing.edition_scope import build_scope_contract
from src.processing.evidence_sampling import (
    FragmentWithContext,
    RepresentativeEvidenceSampler,
    SampledFragment,
)
from src.processing.operational_semantics import normalize_service_state_evidence


def test_gate_excerpt_separates_primary_reply_from_parent_context() -> None:
    formatter = getattr(event_triage, "format_gate_fragment_excerpt", None)
    assert callable(formatter), "Gate needs a formatter with a visible source/context boundary"

    formatted = formatter(
        {
            "fragment_id": 452,
            "observed_at": "2026-10-04T08:30:00+00:00",
            "source_role": "community",
            "source_name": "Residents",
            "text": "На третьем пляже его нет третий месяц.",
        },
        "В центре третий день нет света.",
    )

    assert "frag=452" in formatted
    assert "На третьем пляже его нет третий месяц." in formatted
    assert "В центре третий день нет света." in formatted
    parent_line = next(line for line in formatted.splitlines() if "В центре" in line)
    assert "frag=" not in parent_line
    assert "not citable" in parent_line.casefold()


def test_gate_prompt_limits_parent_context_to_reply_interpretation() -> None:
    prompt = event_triage._GATE_V2_SYSTEM_PROMPT.casefold()

    assert "parent context" in prompt
    assert "source_fragment_ids" in prompt
    assert "not as citable support" in prompt


def test_gate_prompt_preserves_direct_report_when_parent_only_resolves_referent() -> None:
    prompt = event_triage._GATE_V2_SYSTEM_PROMPT.casefold()

    assert "the referent unambiguous" in prompt
    assert "publish as a community_report" in prompt
    assert "context solely because it uses a pronoun" in prompt
    assert "reply's own place, status, and duration" in prompt
    assert "по сообщению жителя, на 3-м пляже света нет уже третий месяц" in prompt
    assert "do not call the object unknown" in prompt


def test_gate_prompt_resolves_explicit_city_deixis_only_for_matching_edition_source() -> None:
    prompt = event_triage._GATE_V2_SYSTEM_PROMPT.casefold()

    assert event_triage.TRIAGE_VERSION == "v17"
    assert "generic city-level locator" in prompt
    assert "primary source text itself" in prompt
    assert "source metadata name exactly matches the configured target edition" in prompt
    assert "source name alone does not establish locality" in prompt
    assert "only city-level scope" in prompt
    assert "do not infer a district, street, neighborhood, or more specific service area" in prompt


def test_gate_geographic_context_recognizes_berdyansk_third_beach() -> None:
    scope = load_config("config.yaml").settings.edition_scopes["berdyansk"]
    geography = resolve_edition_geography("berdyansk", scope.name)

    contract = build_scope_contract(scope, geography)

    assert "Третий пляж" in contract
    assert "3-й пляж" in contract
    assert "Слободка" in contract


def test_analysis_excerpt_marks_the_exact_fragment_as_its_source() -> None:
    formatter = getattr(event_analysis, "format_analysis_fragment_excerpt", None)
    assert callable(formatter), "Analysis needs an explicit source-fragment boundary"

    formatted = formatter(
        fragment_id=37965,
        timestamp="2026-10-04 08:30 UTC",
        role_tag="[COMMUNITY]",
        source_name="Residents",
        text="На третьем пляже его нет третий месяц.",
    )

    assert "fragment_id=37965" in formatted
    assert "PRIMARY SOURCE TEXT" in formatted
    assert "На третьем пляже его нет третий месяц." in formatted


def test_analysis_excerpt_labels_parent_as_non_citable_interpretive_context() -> None:
    formatted = event_analysis.format_analysis_fragment_excerpt(
        fragment_id=63039,
        timestamp="2026-10-04 08:30 UTC",
        role_tag="[COMMUNITY]",
        source_name="Residents",
        text="Да, воду дали в 20:00.",
        reply_parent_context_text="Во всём городе сегодня дают воду?",
    )

    assert "PRIMARY SOURCE TEXT: Да, воду дали в 20:00." in formatted
    assert "REPLY-PARENT CONTEXT ONLY — NOT CITABLE" in formatted
    parent_line = next(line for line in formatted.splitlines() if "Во всём городе" in line)
    assert "source_fragment_id=" not in parent_line


def test_analysis_prompt_limits_parent_to_referent_and_never_to_claim_evidence() -> None:
    prompt = event_analysis._EVENT_ANALYSIS_SYSTEM_PROMPT.casefold()

    assert "reply-parent context only" in prompt
    assert "may clarify the reply's subject or location only when" in prompt
    assert "the unique question the reply directly answers" in prompt
    assert "parent context is never evidence that any event or status occurred" in prompt
    assert "do not infer a fragment's service from its story topic" in prompt
    assert (
        "must never support the reply's status, duration, cause, number, or completion state"
        in prompt
    )
    assert "a direct reply's own words remain citable under its own fragment id" in prompt


def test_analysis_normalization_passes_parent_context_without_citing_it() -> None:
    payload = EventPayload(
        evidence_items=(
            EvidenceItemPayload(
                text="Воду дали больше 20 минут назад.",
                kind="service_access",
                publication_use="PUBLISH",
                source_fragment_ids=(63039,),
                service_state=ServiceStatePayload(
                    subject_key="water_supply",
                    subject_label="Водоснабжение",
                    dimension="availability",
                    state="AVAILABLE",
                    expected_now=True,
                    basis="normal_operation",
                ),
            ),
        )
    )
    sampled = (
        SampledFragment(
            fragment_id=63039,
            text_content="Да, дали больше 20 минут назад.",
            source_id=1,
            source_name="Бердянск",
            source_type="community",
            timestamp=event_analysis.dt.datetime(
                2026, 10, 4, tzinfo=event_analysis.dt.timezone.utc
            ),
            similarity_to_centroid=1.0,
            is_official=False,
            reply_parent_context_text="Во всём городе воду дали?",
        ),
    )

    normalized, audit = event_analysis._normalize_analysis_payload(payload, sampled)

    reply = normalized.evidence_items[0]
    assert reply.publication_use == "PUBLISH"
    assert reply.kind == "community_report"
    assert reply.text == "Да, дали больше 20 минут назад."
    assert reply.source_fragment_ids == (63039,)
    assert reply.service_state is None
    assert audit.rejection_reasons == ("reply_context_only_subject",)


def test_representative_sampler_preserves_reply_parent_context() -> None:
    fragment = SourceFragment(
        id=63039,
        source_item_revision_id=77,
        ordinal=0,
        text_content="Да, воду дали в 20:00.",
        normalized_hash="hash",
        fragmenter_version="v1",
        is_candidate=True,
        drop_reason=None,
        created_at=dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc),
    )
    context = FragmentWithContext(
        fragment=fragment,
        vector=(1.0, 0.0),
        source_id=1,
        source_name="Бердянск",
        source_type="community",
        timestamp=dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc),
        reply_parent_context_text="Во всём городе воду дали?",
    )

    sampled = RepresentativeEvidenceSampler().sample_fragments(
        [context], centroid=(1.0, 0.0), limit=1
    )

    assert sampled[0].reply_parent_context_text == "Во всём городе воду дали?"


def test_analysis_does_not_infer_service_from_an_ambiguous_parent_question() -> None:
    payload = EventPayload(
        evidence_items=(
            EvidenceItemPayload(
                text="На РТС третий день нет света.",
                kind="service_access",
                publication_use="PUBLISH",
                source_fragment_ids=(51308,),
                service_state=ServiceStatePayload(
                    subject_key="power_supply",
                    subject_label="Электроснабжение",
                    dimension="availability",
                    state="UNAVAILABLE",
                    expected_now=True,
                    basis="direct_failure",
                ),
            ),
        )
    )
    sampled = (
        SampledFragment(
            fragment_id=51308,
            text_content="Нет, уже 3 день",
            source_id=1,
            source_name="Бердянск",
            source_type="community",
            timestamp=dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc),
            similarity_to_centroid=1.0,
            is_official=False,
            reply_parent_context_text="РТС есть?",
        ),
    )

    normalized, _ = event_analysis._normalize_analysis_payload(payload, sampled)

    reply = normalized.evidence_items[0]
    assert reply.publication_use == "CONTEXT"
    assert reply.source_fragment_ids == (51308,)
    assert reply.service_state is None


def test_unstructured_service_claim_cannot_borrow_subject_from_story_topic() -> None:
    payload = EventPayload(
        evidence_items=(
            EvidenceItemPayload(
                text="Житель описал режим: неделю без электричества, затем сутки с подачей.",
                kind="community_report",
                publication_use="PUBLISH",
                source_fragment_ids=(101220,),
            ),
        )
    )

    sampled = (
        SampledFragment(
            fragment_id=101220,
            text_content="У нас неделю нет сутки есть",
            source_id=1,
            source_name="Бердянск",
            source_type="community",
            timestamp=dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc),
            similarity_to_centroid=1.0,
            is_official=False,
            reply_parent_context_text="Через две недели? Я уже график высчитала👍 А ещё таймер на вечер просто 🤣",
        ),
    )

    normalized, audit = event_analysis._normalize_analysis_payload(payload, sampled)

    claim = normalized.evidence_items[0]
    assert claim.publication_use == "CONTEXT"
    assert claim.source_fragment_ids == (101220,)
    assert audit.rejection_reasons == ("ungrounded_service_family",)


def test_unstructured_service_claim_can_use_direct_parent_question_for_referent() -> None:
    payload = EventPayload(
        evidence_items=(
            EvidenceItemPayload(
                text="На Горе пятый день нет электричества.",
                kind="community_report",
                publication_use="PUBLISH",
                source_fragment_ids=(34531,),
            ),
        )
    )

    normalized, _ = normalize_service_state_evidence(
        payload,
        {34531: "У нас 5 день нет"},
        reply_parent_context_by_fragment_id={34531: "На горе есть свет ?"},
    )

    claim = normalized.evidence_items[0]
    assert claim.publication_use == "PUBLISH"
    assert claim.source_fragment_ids == (34531,)


def test_unstructured_service_reply_preserves_recent_availability_report() -> None:
    payload = EventPayload(
        evidence_items=(
            EvidenceItemPayload(
                text="На АКЗ свет был недавно.",
                kind="community_report",
                publication_use="PUBLISH",
                source_fragment_ids=(39820,),
            ),
        )
    )

    normalized, _ = normalize_service_state_evidence(
        payload,
        {39820: "У вас был недавно. Очередь следующих"},
        reply_parent_context_by_fragment_id={39820: "На АКЗ не появился свет?"},
    )

    reply = normalized.evidence_items[0]
    assert reply.publication_use == "PUBLISH"
    assert reply.kind == "community_report"
    assert reply.source_fragment_ids == (39820,)


def test_unstructured_service_reply_preserves_citywide_electricity_report() -> None:
    payload = EventPayload(
        evidence_items=(
            EvidenceItemPayload(
                text="По сообщению жителя, весь город без света.",
                kind="service_access",
                publication_use="PUBLISH",
                source_fragment_ids=(111995,),
                service_state=ServiceStatePayload(
                    subject_key="power_supply",
                    subject_label="Электроснабжение",
                    dimension="availability",
                    state="UNAVAILABLE",
                    expected_now=True,
                    basis="direct_failure",
                ),
            ),
        )
    )

    normalized, _ = normalize_service_state_evidence(
        payload,
        {111995: "Весь город офф"},
        reply_parent_context_by_fragment_id={
            111995: "Я так понял, что свет везде вырубили? У кого-то есть свет без генератора?"
        },
    )

    reply = normalized.evidence_items[0]
    assert reply.publication_use == "PUBLISH"
    assert reply.kind == "community_report"
    assert reply.text == "Весь город офф"
    assert reply.source_fragment_ids == (111995,)
    assert reply.service_state is None


def test_citywide_reply_may_name_only_the_target_edition() -> None:
    payload = EventPayload(
        evidence_items=(
            EvidenceItemPayload(
                text="По сообщению жителя, света нет во всём Бердянске.",
                kind="service_access",
                publication_use="PUBLISH",
                source_fragment_ids=(111995,),
                service_state=ServiceStatePayload(
                    subject_key="power_supply",
                    subject_label="Электроснабжение",
                    dimension="availability",
                    state="UNAVAILABLE",
                    expected_now=True,
                    basis="direct_failure",
                ),
            ),
        )
    )

    normalized, _ = normalize_service_state_evidence(
        payload,
        {111995: "Весь город офф"},
        reply_parent_context_by_fragment_id={
            111995: "Я так понял, что свет везде вырубили? У кого-то есть свет без генератора?"
        },
        edition_name="Бердянск",
    )

    reply = normalized.evidence_items[0]
    assert reply.publication_use == "PUBLISH"
    assert reply.text == "Весь город офф"
    assert reply.source_fragment_ids == (111995,)


def test_citywide_reply_cannot_be_relabelled_as_another_city() -> None:
    payload = EventPayload(
        evidence_items=(
            EvidenceItemPayload(
                text="По сообщению жителя, света нет во всём Мелитополе.",
                kind="service_access",
                publication_use="PUBLISH",
                source_fragment_ids=(111995,),
            ),
        )
    )

    normalized, _ = normalize_service_state_evidence(
        payload,
        {111995: "Весь город офф"},
        reply_parent_context_by_fragment_id={
            111995: "Я так понял, что свет везде вырубили? У кого-то есть свет без генератора?"
        },
        edition_name="Бердянск",
    )

    assert normalized.evidence_items[0].publication_use == "CONTEXT"


def test_unstructured_service_claim_checks_each_cited_reply() -> None:
    payload = EventPayload(
        evidence_items=(
            EvidenceItemPayload(
                text="Жители Горы и района РТС сообщили о перебоях со светом.",
                kind="community_report",
                publication_use="PUBLISH",
                source_fragment_ids=(34531, 101220),
            ),
        )
    )

    normalized, _ = normalize_service_state_evidence(
        payload,
        {
            34531: "У нас 5 день нет",
            101220: "У нас неделю нет сутки есть",
        },
        reply_parent_context_by_fragment_id={
            34531: "На горе есть свет ?",
            101220: "Через две недели? Я уже график высчитала",
        },
    )

    assert normalized.evidence_items[0].publication_use == "CONTEXT"


def test_reply_detail_matching_does_not_confuse_eleven_with_one_day() -> None:
    payload = EventPayload(
        evidence_items=(
            EvidenceItemPayload(
                text="На Горе света нет первый день.",
                kind="community_report",
                publication_use="PUBLISH",
                source_fragment_ids=(34531,),
            ),
        )
    )

    normalized, _ = normalize_service_state_evidence(
        payload,
        {34531: "У нас нет уже 11 дней"},
        reply_parent_context_by_fragment_id={34531: "На горе есть свет?"},
    )

    assert normalized.evidence_items[0].publication_use == "CONTEXT"


def test_analysis_prompt_requires_each_claim_to_match_its_cited_fragment() -> None:
    prompt = event_analysis._EVENT_ANALYSIS_SYSTEM_PROMPT.casefold()

    assert event_analysis.ANALYSIS_VERSION == "v10"
    assert "source_fragment_ids" in prompt
    assert "directly supports" in prompt


def test_reply_parent_can_anchor_a_short_answer_without_becoming_its_evidence() -> None:
    parameters = inspect.signature(normalize_service_state_evidence).parameters
    assert "reply_parent_context_by_fragment_id" in parameters, (
        "service-state validation must receive reply context separately from primary source text"
    )

    payload = EventPayload(
        evidence_items=(
            EvidenceItemPayload(
                text="На Крылова свет появился около десяти минут назад.",
                kind="service_access",
                publication_use="PUBLISH",
                source_fragment_ids=(452,),
                service_state=ServiceStatePayload(
                    subject_key="power_supply",
                    subject_label="Электроснабжение",
                    dimension="availability",
                    state="AVAILABLE",
                    expected_now=True,
                    basis="normal_operation",
                ),
            ),
        )
    )
    normalized, audit = normalize_service_state_evidence(
        payload,
        {452: "Да, минут десять назад."},
        reply_parent_context_by_fragment_id={452: "На Крылова свет появился?"},
    )

    reply = normalized.evidence_items[0]
    assert reply.publication_use == "PUBLISH"
    assert reply.kind == "community_report"
    assert reply.text == "Да, минут десять назад."
    assert reply.service_state is None
    assert audit.rejection_reasons == ("reply_context_only_subject",)


def test_dependent_service_report_keeps_its_own_place_and_duration() -> None:
    payload = EventPayload(
        evidence_items=(
            EvidenceItemPayload(
                text="На Третьем пляже электричества нет уже третий месяц.",
                kind="service_access",
                publication_use="PUBLISH",
                source_fragment_ids=(455,),
                service_state=ServiceStatePayload(
                    subject_key="power_supply",
                    subject_label="Электроснабжение",
                    dimension="availability",
                    state="UNAVAILABLE",
                    location="Третий пляж, Бердянск",
                    expected_now=True,
                    basis="direct_failure",
                ),
            ),
        )
    )
    normalized, audit = normalize_service_state_evidence(
        payload,
        {455: "На 3 пляже его уже нету третий месяц😂😂"},
        reply_parent_context_by_fragment_id={
            455: "В центре в другой половине, света уже нет 3 суток"
        },
    )

    reply = normalized.evidence_items[0]
    assert reply.publication_use == "PUBLISH"
    assert reply.kind == "community_report"
    assert reply.text == "На 3 пляже его уже нету третий месяц😂😂"
    assert reply.service_state is None
    assert audit.rejection_reasons == ("reply_context_only_subject",)


def test_parent_only_service_claim_is_not_promoted_from_an_unrelated_reply() -> None:
    payload = EventPayload(
        evidence_items=(
            EvidenceItemPayload(
                text="В центре третий день нет света.",
                kind="service_access",
                publication_use="PUBLISH",
                source_fragment_ids=(453,),
                service_state=ServiceStatePayload(
                    subject_key="power_supply",
                    subject_label="Электроснабжение",
                    dimension="availability",
                    state="UNAVAILABLE",
                    expected_now=True,
                    basis="direct_failure",
                ),
            ),
        )
    )
    normalized, _ = normalize_service_state_evidence(
        payload,
        {453: "На третьем пляже его нет уже третий месяц."},
        reply_parent_context_by_fragment_id={453: "В центре третий день нет света."},
    )

    reply = normalized.evidence_items[0]
    assert reply.publication_use == "CONTEXT"
    assert reply.text == "В центре третий день нет света."
    assert reply.service_state is None


def test_direct_service_report_remains_publishable_with_its_own_source() -> None:
    payload = EventPayload(
        evidence_items=(
            EvidenceItemPayload(
                text="На Крылова больше десяти дней нет света.",
                kind="service_access",
                publication_use="PUBLISH",
                source_fragment_ids=(454,),
                service_state=ServiceStatePayload(
                    subject_key="power_supply",
                    subject_label="Электроснабжение",
                    dimension="availability",
                    state="UNAVAILABLE",
                    expected_now=True,
                    basis="direct_failure",
                ),
            ),
        )
    )
    normalized, _ = normalize_service_state_evidence(
        payload, {454: "На Крылова больше десяти дней нет света."}
    )

    report = normalized.evidence_items[0]
    assert report.publication_use == "PUBLISH"
    assert report.kind == "service_access"
    assert report.service_state is not None
