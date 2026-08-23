# Decision Record: Telegram Publication Architecture & Rollback Strategy

**Date**: 2026-08-23
**Status**: Decided (Adopt `knowledge_full` and `knowledge_no_embeddings` knowledge modes; retain legacy rollback as an explicit deferred adapter)

---

## 1. Context & Motivation

During the multisource architecture transition, Telegram digest generation moved from direct, unindexed message scraping (live Telethon queries during publication runs) to the canonical multi-source knowledge platform (`sources -> source_items -> revisions -> claims -> places -> stories -> story_revisions -> publication_runs`).

To guarantee operational resilience when external embedding providers (e.g. Gemini / OpenAI) are unavailable or when operators run with zero external embedding dependencies, a zero-vector retrieval mode (`telegram.processing_mode = knowledge_no_embeddings`) was implemented in Milestone D (Task 8).

This document evaluates the comparison between `knowledge_no_embeddings` and the legacy direct-message pipeline (`custom`), establishing the long-term rollback and operational strategy.

---

## 2. Comparative Evaluation: `knowledge_no_embeddings` vs Legacy `custom`

| Criterion | Legacy Pipeline (`custom`) | Multi-Source Mode (`knowledge_no_embeddings`) | Multi-Source Mode (`knowledge_full`) |
| :--- | :--- | :--- | :--- |
| **Data Flow** | Live Telethon pull during digest job execution | Async ingestion into PostgreSQL -> incremental Claim & Place extraction -> lexical & entity clustering | Ingestion -> Claim & Place extraction -> pgvector semantic retrieval + lexical + place recall |
| **External Dependencies** | Telegram Telethon API connection active at publication time | Zero external embedding API calls; uses PostgreSQL full-text and local entity overlap | Vector embeddings API + LLM editorial selector |
| **Attribution & Provenance** | Loose message ID lists; easily lost during summarization | Exact immutable provenance: `Claim` -> `SourceItemRevision` -> `SourceItem` -> `Source` with edition isolation | Exact immutable provenance: `Claim` -> `SourceItemRevision` -> `SourceItem` -> `Source` |
| **False Merges / Clustering** | High risk: monolithic prompt attempts cross-channel deduplication in single context window | Low risk: structured `StoryMatchingService` evaluates candidates against edition stories with bounded lexical recall | Very low risk: combined semantic cosine distance + lexical overlap + spatial place boundaries |
| **Locally Useful Items** | Prone to context window truncation when volume exceeds token budgets | High capture rate: granular claim extraction preserves independent municipal, emergency, and utility propositions | High capture rate: granular claim extraction + dense semantic search |
| **Operational Isolation** | Monolithic: any Telethon rate limit or network glitch fails publication entirely | Decoupled: Procrastinate background queue isolates collection, extraction, place resolution, story matching, and publication | Decoupled Procrastinate queues |

---

## 3. Decision & Policy

1. **Retain the Dual Knowledge Modes (`knowledge_full` & `knowledge_no_embeddings`)**:
   - The production publication pipeline standardizes on the Knowledge Domain (`knowledge_editorial_adapter.py` / `publication_runs`).
   - In environments where vector embedding models are enabled, `knowledge_full` leverages dense vector search alongside lexical and spatial indexing.
   - When external embedding providers are disabled or unavailable, `knowledge_no_embeddings` operates entirely within local PostgreSQL storage via full-text search (`to_tsvector`), state recency, and place/entity overlap without incurring embedding costs or network failures.

2. **Decommission Direct Live Telethon Publication**:
   - Recreating a second, parallel live-Telethon publication architecture is rejected as it breaks auditability, violates database invariants, and reintroduces runtime coupling with Telegram rate limits.

3. **Fallback & Legacy Parity Protocol**:
   - If exact legacy message-level feed parity is ever required for historical reconciliation, it will be implemented via an offline persisted-history adapter reading from `source_items` / `source_item_revisions` in PostgreSQL, never via live Telethon network calls during publication.
