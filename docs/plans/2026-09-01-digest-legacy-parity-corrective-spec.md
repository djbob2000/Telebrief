# Corrective Spec: Truthful Source Binding, Reader Text Tracing, and Parity Audits

## Context and Problem

The previous regression evaluation and exporter had several architectural flaws:
1. False-green microdetail retention: `has_final` was derived from candidate/evidence presence or loose story tags, meaning microdetails could be marked as retained even if completely missing from the generated digest text.
2. Incomplete exporter: Exporter queried source fragments only through existing formed stories, dropping unselected source fragments.
3. Missing presentation plan audit: `DigestPresentationPlan` was not persisted in publication metadata, making it impossible to audit deterministic dashboard decisions vs LLM writer decisions offline.
4. Single combined fixture for article and digest: Evaluated digest against a 12-unit article floor fixture without binding source matchers.

## Architecture and Design Changes

### 1. Reader-Visible Coverage Text Tracing
- `DigestStoryCoverageTrace` now records `dashboard_texts: tuple[str, ...]` and `detail_texts: tuple[str, ...]` populated from the exact deterministic rendering (`city_situation_group_reader_text`) and thematic items (`f"{item.headline}: {item.body}"`).
- `evaluate_case()` checks microdetail retention against `final_trace_units` containing the exact canonical published reader text.

### 2. Publication Metadata Audit Persistence
- `DigestPresentationPlan.to_audit_dict()` is persisted into `pub.metadata["digest_presentation_plan"]` on both AI and deterministic generation paths.
- Missing presentation plan metadata in digest evaluations triggers `MISSING_DIGEST_PRESENTATION_PLAN_METADATA` and fails parity verification.

### 3. Real Raw Source Corpus Exporter
- `export_source_corpus_for_window()` queries raw source fragments directly for the edition within the lookback window.
- Computes stable fixture fragment IDs using `stable_fixture_fragment_id(platform, source_external_id, item_external_id, text)`.

### 4. Mandatory Source Binding and Fixture Split
- Separate fixtures for digest (`berdyansk_2026_09_01_digest_legacy_floor.json`) and article (`berdyansk_2026_08_31_legacy_floor.json`).
- Source matcher manifests (`berdyansk_2026_09_01_digest_source_matchers.json`, `berdyansk_2026_08_31_article_source_matchers.json`).
- `scripts/bind_legacy_floor_sources.py` binds semantic units to raw source fragments and rejects unbound units during evaluation.

### 5. Parity Benchmark CLI
- `scripts/benchmark_publication_floor_parity.py` evaluates Event-First runs against frozen legacy floors and outputs a detailed stage-by-stage and microdetail breakdown.
