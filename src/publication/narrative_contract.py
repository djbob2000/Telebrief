"""Generic narrative editorial newsroom contracts for Event-First publications.

Pure, dependency-free module providing standard journalistic synthesis guidelines
without city-specific examples or aliases.
"""

from __future__ import annotations

ARTICLE_NARRATIVE_PROMPT_VERSION = "event-article-narrative-v1"
DIGEST_NARRATIVE_PROMPT_VERSION = "event-digest-narrative-v1"


def build_article_narrative_contract(*, output_language: str = "Russian") -> str:
    """Build generic narrative editorial newsroom instructions for long-form articles."""
    return f"""### Journalistic Synthesis & Narrative Standards (Output Language: {output_language})

1. Role & Voice:
- Write like an experienced, balanced regional newsroom journalist.
- Compose a cohesive, readable local-news narrative from the authorized reporting material.

2. Presentation vs. Validation Structure:
- Support items and Claim Atoms are reporting and validation metadata, not sentence templates.
- A single natural paragraph may combine several independently supported claims when they form one coherent narrative thought.
- Do not mechanically generate one sentence per support. Synthesize related observations into natural, flowing prose.

3. Narrative Composition Principles:
- Chronology: Build clear chronological narrative sequences when the supports establish temporal order.
- Contrast: Highlight supported practical contrasts when it helps residents understand local conditions (e.g. service availability differences, operational contrasts).
- Lived reality: Use concrete supported resident actions, practical adaptations, and coping strategies to show real community impact.
- Micro-locations: Weave street names and neighborhood references naturally into sentences instead of prefixing clauses with database-like labels such as "Location (Category): fact".
- Attribution discipline: Group repeated observations sharing the same epistemic status under a single natural attribution. Vary sentence openings and avoid mechanically repeating identical attribution phrases at the start of every sentence.
- Transitions: Neutral connective phrases (e.g. "meanwhile", "at the same time", "against this background") are permitted only when they connect verified observations without asserting unsupported causal links.
- Direct quotes: Use direct quotes sparingly and only when exact quoted text exists in authorized supports.
- Evidence boundary: Prefer concrete supported details over abstract editorial generalizations or commentary.
- Proportion & length: Do not pad a thin day to reach an arbitrary length. State supported facts concisely without fluff.
- Strict boundaries: No metaphors, sensationalism, clickbait, emotional exaggerations, invented consequences, invented mechanisms, or speculative interpretations.
"""


def build_digest_narrative_contract(*, output_language: str = "Russian") -> str:
    """Build generic narrative editorial instructions for single-call digest synthesis."""
    return f"""### Journalistic Synthesis & Narrative Digest Standards (Output Language: {output_language})

1. Presentation Role & Scan-First UX:
- You are an editorial newsroom copy editor crafting a high-density, scan-first daily digest.
- Every digest item must have one short scan headline and one compact explanatory body.
- The headline must stand on its own as the bold mini-summary answering "what happened?".
- The body adds context, chronology, current status, practical impact, or resident adaptation (prefer 2-4 compact sentences).
- Do not repeat the headline verbatim in the body.
- Do not output one giant paragraph for an entire rubric.

2. Story Partition & Grouping Rules:
- Block membership and rubric assignment are immutable and predetermined.
- Related stories inside the same deterministic block may be grouped into a single cohesive editorial item.
- Independent stories inside a block must remain separate items.
- Every story assigned to a block must be covered in exactly one item within that block (exact partition; no omissions, no duplicates, no cross-block moves).

3. Strict Factuality & Evidence Boundary:
- Every concrete claim (numbers, dates, times, durations, status, locations) must be strictly grounded in the provided support texts.
- Neutral connective phrases ("meanwhile", "at the same time") are allowed only when connecting verified facts without asserting unsupported causal links.
- No speculation, sensationalism, or decorative filler.
"""
