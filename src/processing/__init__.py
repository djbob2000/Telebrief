"""Knowledge processing services: AI relevance decisions (Plan 3 Task 2).

Conservative fail-open semantics: when the AI provider is unavailable the
pipeline persists an ``uncertain`` decision instead of guessing ``irrelevant``.
Decisions are immutable; duplicate executions converge on the canonical root
row enforced by uq_root_relevance_decision.
"""
