"""Editorial and publication configuration schemas."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_DIGEST_RUBRIC_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
EVENT_PIPELINE_MODES = ("event_first",)
ALLOWED_REASONING_EFFORTS = {"none", "low", "medium", "high"}


@dataclass
class DigestGroupConfig:
    """Configuration for a single digest topic group."""

    name: str
    description: str
    prompt_extra: str = ""  # appended to system prompt for channels in this group


@dataclass(frozen=True)
class DigestRubricConfig:
    """Configuration for a single digest presentation rubric."""

    id: str
    name: str
    description: str
    emoji: str = ""
    fallback: bool = False


DEFAULT_DIGEST_RUBRIC = DigestRubricConfig(
    id="other",
    name="Другое",
    description="Важные локальные события",
    emoji="📌",
    fallback=True,
)


@dataclass(frozen=True)
class DigestRubricsConfig:
    """Edition-level configuration for digest display rubrics."""

    min_similarity: float = 0.38
    items: tuple[DigestRubricConfig, ...] = (DEFAULT_DIGEST_RUBRIC,)

    @property
    def fallback(self) -> DigestRubricConfig:
        matches = [item for item in self.items if item.fallback]
        if len(matches) != 1:
            raise ValueError("digest_rubrics must contain exactly one fallback rubric")
        return matches[0]


@dataclass(frozen=True)
class EditionScopeConfig:
    name: str
    focus_places: tuple[str, ...]
    direct_impact_only: bool = True
    notes: tuple[str, ...] = ()


@dataclass
class ArticleConfig:
    """Configuration for daily editorial article generation."""

    enabled: bool = True
    schedule_time: str = "20:00"
    lookback_hours: int = 24
    author_name: str = "Бердянск Новости"
    fallback_save_dir: str = "data/articles"
    prompt_template: str = "src/prompts/news_style.md"
    generation_retries: int = 2  # Deprecated: retained for schema backwards compatibility
    generation_retry_delay: float = 1.0  # Deprecated: retained for schema backwards compatibility
    editorial_max_output_tokens: int = 65_536
    editorial_analysis_max_output_tokens: int = 65_536
    editorial_analysis_compact_max_output_tokens: int = 16_384
    editorial_writer_max_output_tokens: int = 65_536
    editorial_audit_max_output_tokens: int = 32_768
    editorial_repair_max_output_tokens: int = 8_192
    editorial_api_timeout: int = 300
    telegraph_access_token: str | None = None
    save_debug_artifacts: bool = False
    debug_artifact_dir: str = "data/debug/editorial"
    temperature: float | None = None


@dataclass
class ArticleScheduleConfig:
    """Configuration for periodic longitudinal articles (weekly/monthly)."""

    enabled: bool = True
    schedule_day: str | int = "sunday"  # "sunday" or 1
    schedule_time: str = "19:00"
    target_word_count: int = 2000
    lookback_hours: int = 168


@dataclass(frozen=True)
class EventPipelineConfig:
    """Cost-bounded event-first processing pipeline configuration."""

    mode: Literal["event_first"] = "event_first"

    fragment_max_chars: int = 1200
    active_window_hours: int = 72
    join_similarity: float = 0.84
    max_cluster_candidates: int = 20
    embedding_batch_size: int = 128
    direct_analysis_min_fragments: int = 3
    direct_analysis_min_unique_sources: int = 2
    triage_batch_size: int = 25
    triage_max_output_tokens: int = 12_288
    triage_reasoning_effort: str | None = "low"
    analysis_max_output_tokens: int = 8_192
    analysis_reasoning_effort: str | None = "low"
    triage_max_attempts_per_assignment: int = 2
    analysis_max_attempts_per_assignment: int = 2
    triage_excerpt_chars: int = 320
    triage_min_ignore_confidence: float = 0.95
    analysis_quiet_seconds: int = 120
    analysis_min_interval_seconds: int = 600
    analysis_min_new_fragments: int = 3
    analysis_max_calls_per_story_per_hour: int = 4
    provider_retry_backoff_seconds: int = 300
    provider_retry_backoff_max_seconds: int = 3_600
    analysis_max_input_chars: int = 24000
    representative_fragment_limit: int = 16
    rich_analysis_max_calls_per_cycle: int = 40
    event_processing_cycle_lease_seconds: int = 600
    authority_coordination_lease_seconds: int = 10
    event_processing_stage_lease_seconds: int = 600
    triage_split_max_extra_calls_per_cycle: int = 8
    live_batch_size: int = 100
    backfill_batch_size: int = 500

    def __post_init__(self) -> None:
        for field_name in (
            "triage_max_output_tokens",
            "analysis_max_output_tokens",
            "triage_max_attempts_per_assignment",
            "analysis_max_attempts_per_assignment",
            "provider_retry_backoff_max_seconds",
            "event_processing_cycle_lease_seconds",
            "authority_coordination_lease_seconds",
            "event_processing_stage_lease_seconds",
            "triage_split_max_extra_calls_per_cycle",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        for field_name in ("triage_reasoning_effort", "analysis_reasoning_effort"):
            effort = getattr(self, field_name)
            if effort is not None and effort not in ALLOWED_REASONING_EFFORTS:
                raise ValueError(
                    f"{field_name} must be one of {sorted(ALLOWED_REASONING_EFFORTS)} or null"
                )


@dataclass(frozen=True)
class PublicationEditorialConfig:
    """Editorial parameters for publication digests and articles."""

    conflict_window_minutes: int = 90
    article_min_words: int = 800
    article_max_words: int = 2200
    article_min_sections: int = 3
    article_max_sections: int = 8
    article_max_direct_quotes: int = 4

    article_claim_min_content_coverage: float = 0.50
    digest_narrative_mode: str = "deterministic"
    digest_narrative_max_cards_per_block: int = 6

    digest_narrative_max_output_tokens: int = 4096
    selection_max_output_tokens: int = 4096
    selection_reasoning_effort: str | None = "low"
    digest_city_situation_max_items: int = 7
    digest_city_situation_max_details_per_item: int = 2
    digest_city_situation_max_positive_items: int = 2
    article_allow_deterministic_fallback: bool = False
    article_editor_enabled: bool = False
    article_editor_max_attempts: int = 2

    def __post_init__(self) -> None:
        if self.conflict_window_minutes <= 0:
            raise ValueError("conflict_window_minutes must be positive")
        if self.article_min_words <= 0 or self.article_max_words <= 0:
            raise ValueError("article word limits must be positive")
        if self.article_min_words > self.article_max_words:
            raise ValueError("article_min_words cannot be greater than article_max_words")
        if self.article_min_sections <= 0 or self.article_max_sections <= 0:
            raise ValueError("article section limits must be positive")
        if self.article_min_sections > self.article_max_sections:
            raise ValueError("article_min_sections cannot be greater than article_max_sections")
        if self.article_max_direct_quotes < 0:
            raise ValueError("article_max_direct_quotes cannot be negative")
        if not (0.5 <= self.article_claim_min_content_coverage <= 1.0):
            raise ValueError("article_claim_min_content_coverage must be between 0.5 and 1.0")
        if self.digest_narrative_mode not in ("deterministic", "single_call", "journalistic"):
            raise ValueError(
                f"digest_narrative_mode must be 'deterministic', 'single_call', or 'journalistic', got {self.digest_narrative_mode!r}"
            )
        if self.digest_narrative_max_cards_per_block <= 0:
            raise ValueError("digest_narrative_max_cards_per_block must be a positive integer")
        if self.digest_narrative_max_output_tokens <= 0:
            raise ValueError("digest_narrative_max_output_tokens must be a positive integer")
        if self.selection_max_output_tokens <= 0:
            raise ValueError("selection_max_output_tokens must be a positive integer")
        if (
            self.selection_reasoning_effort is not None
            and self.selection_reasoning_effort not in ALLOWED_REASONING_EFFORTS
        ):
            raise ValueError(
                f"selection_reasoning_effort must be one of {sorted(ALLOWED_REASONING_EFFORTS)} or null"
            )
        if not (1 <= self.digest_city_situation_max_items <= 12):
            raise ValueError("digest_city_situation_max_items must be between 1 and 12")
        if not (1 <= self.digest_city_situation_max_details_per_item <= 4):
            raise ValueError("digest_city_situation_max_details_per_item must be between 1 and 4")
        if not (0 <= self.digest_city_situation_max_positive_items <= 4):
            raise ValueError("digest_city_situation_max_positive_items must be between 0 and 4")
