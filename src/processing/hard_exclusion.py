"""Deterministic Gate postcondition policy for hard exclusions.

Evaluates story fragments at fragment-level. A story is dropped (DROP + NONE)
ONLY IF all substantive fragments are hard-excluded. Mixed stories (containing
both civic facts and commercial ads/directory payload) remain KEEP, with the
commercial fragments flagged for EXCLUDE.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from src.processing.noise_detection import classify_text_noise_or_exclusion


@dataclass(frozen=True)
class HardExclusionAudit:
    """Detailed audit of fragment-level evaluation and story-level drop verdict."""

    excluded_fragment_ids: tuple[int, ...]
    retained_fragment_ids: tuple[int, ...]
    exclusion_reasons_by_fragment: dict[int, str] = field(default_factory=dict)
    drop_story: bool = False
    story_exclusion_reason: str | None = None


def evaluate_story_hard_exclusion(
    fragments: Sequence[Mapping[str, Any]],
) -> HardExclusionAudit:
    """Evaluate story fragments individually and determine story-level retention.

    Parameters
    ----------
    fragments : Sequence[Mapping[str, Any]]
        List of fragment dicts, each having at least an ID ('fragment_id' or 'id')
        and text content ('text' or 'text_content').

    Returns
    -------
    HardExclusionAudit
        Audit result with excluded/retained fragment IDs and story drop verdict.
    """
    if not fragments:
        return HardExclusionAudit(
            excluded_fragment_ids=(),
            retained_fragment_ids=(),
            drop_story=True,
            story_exclusion_reason="obvious_noise",
        )

    excluded_ids: list[int] = []
    retained_ids: list[int] = []
    reasons_by_id: dict[int, str] = {}

    for frag in fragments:
        raw_id = frag.get("fragment_id", frag.get("id"))
        try:
            fid = int(raw_id) if raw_id is not None else 0
        except (ValueError, TypeError):
            fid = 0

        text = str(frag.get("full_text", frag.get("text", frag.get("text_content", ""))) or "")
        is_ex, reason = classify_text_noise_or_exclusion(text)

        if is_ex:
            excluded_ids.append(fid)
            if reason:
                reasons_by_id[fid] = reason
        else:
            retained_ids.append(fid)

    # Story-level DROP contract:
    # Only drop the entire story if NO legitimate substantive fragments remain
    drop_story = len(retained_ids) == 0

    story_exclusion_reason: str | None = None
    if drop_story:
        # Determine dominant exclusion reason
        # Priority: commercial_classified > directory_payload > private_classified > obvious_noise
        reason_set = set(reasons_by_id.values())
        if "commercial_classified" in reason_set:
            story_exclusion_reason = "commercial_classified"
        elif "directory_payload" in reason_set:
            story_exclusion_reason = "directory_payload"
        elif "private_classified" in reason_set:
            story_exclusion_reason = "private_classified"
        elif reason_set:
            story_exclusion_reason = next(iter(reason_set))
        else:
            story_exclusion_reason = "obvious_noise"

    return HardExclusionAudit(
        excluded_fragment_ids=tuple(excluded_ids),
        retained_fragment_ids=tuple(retained_ids),
        exclusion_reasons_by_fragment=reasons_by_id,
        drop_story=drop_story,
        story_exclusion_reason=story_exclusion_reason,
    )
