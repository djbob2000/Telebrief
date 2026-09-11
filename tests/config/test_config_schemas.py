import pytest

from src.config.schemas.common import (
    CollectionConfig,
    DatabaseConfig,
    EmbeddingConfig,
)
from src.config.schemas.publication import (
    ArticleConfig,
    EventPipelineConfig,
    PublicationEditorialConfig,
)
from src.config.schemas.telegram import (
    ChannelConfig,
    SourceRoleResolver,
    effective_source_type,
)


def test_schema_instantiation_defaults():
    collection = CollectionConfig()
    assert collection.telegram_interval_minutes == 45

    db = DatabaseConfig()
    assert db.enabled is False
    assert db.min_pool_size == 1

    emb = EmbeddingConfig()
    assert emb.provider == "google"
    assert emb.dimensions == 1536

    article = ArticleConfig()
    assert article.enabled is True
    assert article.editorial_max_output_tokens == 65_536

    pipeline = EventPipelineConfig()
    assert pipeline.mode == "event_first"
    assert pipeline.background_authority_enabled is False
    assert pipeline.fragment_max_chars == 1200
    assert pipeline.authority_coordination_lease_seconds == 10
    assert pipeline.authority_provider_timeout_seconds == 540


def test_event_pipeline_uses_safe_gate_batch_default():
    assert EventPipelineConfig().triage_batch_size == 10


@pytest.mark.parametrize("value", [0, 11, 80])
def test_event_pipeline_rejects_unsafe_gate_batch_size(value):
    with pytest.raises(ValueError, match="triage_batch_size"):
        EventPipelineConfig(triage_batch_size=value)


def test_authority_provider_timeout_is_strictly_below_stage_lease():
    try:
        EventPipelineConfig(
            event_processing_stage_lease_seconds=600,
            authority_provider_timeout_seconds=600,
        )
    except ValueError as exc:
        assert "authority_provider_timeout_seconds" in str(exc)
    else:
        raise AssertionError("authority provider timeout must be below the stage lease")

    from src.config.schemas.root import Settings

    settings = Settings(schedule_time="09:00", timezone="UTC", lookback_hours=24, openai_model="m")
    assert settings.publication_freshness_ttl_minutes == 30
    assert settings.pre_publish_lead_minutes == 30
    assert settings.publication_snapshot_lag_minutes == 0
    assert settings.publication_readiness_deadline_minutes == 20
    assert settings.publication_readiness_on_deadline == "fail_closed"


def test_publication_editorial_validation():
    pub = PublicationEditorialConfig()
    assert pub.conflict_window_minutes == 90
    assert pub.digest_narrative_mode == "deterministic"


def test_source_role_resolver():
    ch = ChannelConfig(id="@test", name="Test Channel", source_type="news")
    resolver = SourceRoleResolver([ch])
    assert resolver.resolve("Test Channel") == "news"
    assert effective_source_type(ch) == "news"


def test_openrouter_models_config_schema():
    from src.config.schemas.root import Config

    cfg = Config(
        channels=[],
        settings=None,  # type: ignore
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_bot_token="token",
        openai_api_key="key",
        log_level="INFO",
        openrouter_models=["m1", "m2"],
    )
    assert cfg.openrouter_models == ["m1", "m2"]
    assert cfg.openrouter_model == "openrouter/free"
