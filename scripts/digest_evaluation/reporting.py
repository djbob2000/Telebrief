"""Evidence-based rollout comparison; editorial scores never gate live publications."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

CASES = (
    "rich_utilities",
    "quiet_day",
    "local_contrasts",
    "practical_services",
    "partial_announcements",
    "community_microdetails",
)
HOLDOUT = ("partial_announcements", "community_microdetails")


def _score(row: Mapping[str, Any]) -> float | None:
    scores = row.get("editorial_scores")
    if (
        not isinstance(scores, list)
        or len(scores) != 5
        or any(not isinstance(s, int) or isinstance(s, bool) or not 0 <= s <= 4 for s in scores)
    ):
        return None
    return sum(scores) * 5


def summarize_digest_comparison(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    unmet = []
    variants: dict[str, dict[str, Any]] = {}
    by_variant = {v: [r for r in rows if r.get("variant") == v] for v in ("baseline", "combined")}
    medians: dict[str, dict[str, float]] = {}
    for variant, data in by_variant.items():
        if Counter((r.get("case"), r.get("repeat")) for r in data) != Counter(
            (case, i) for case in CASES for i in (1, 2, 3)
        ):
            unmet.append(f"incomplete_cases:{variant}")
        accepted = [r for r in data if r.get("status") == "accepted"]
        scores = [s for r in accepted if (s := _score(r)) is not None]
        if len(scores) != len(accepted):
            unmet.append(f"editorial_scores_missing:{variant}")
        times = sorted(
            float(r["elapsed_seconds"])
            for r in data
            if isinstance(r.get("elapsed_seconds"), (int, float))
            and not isinstance(r["elapsed_seconds"], bool)
            and math.isfinite(r["elapsed_seconds"])
            and r["elapsed_seconds"] >= 0
        )
        cost = (
            sum(r["cost"] for r in data)
            if data
            and all(
                isinstance(r.get("cost"), (int, float))
                and not isinstance(r["cost"], bool)
                and math.isfinite(r["cost"])
                and r["cost"] >= 0
                for r in data
            )
            else None
        )
        variants[variant] = {
            "total": len(data),
            "accepted": len(accepted),
            "failed_or_rejected": len(data) - len(accepted),
            "editorial_median": statistics.median(scores) if scores else None,
            "editorial_min": min(scores) if scores else None,
            "p95_seconds": times[max(0, math.ceil(len(times) * 0.95) - 1)]
            if times and len(times) == len(data)
            else None,
            "cost_total": cost,
        }
        medians[variant] = {
            case: statistics.median(
                [s for r in accepted if r.get("case") == case and (s := _score(r)) is not None]
            )
            for case in CASES
            if any(r.get("case") == case and _score(r) is not None for r in accepted)
        }
        for r in accepted:
            if (
                r.get("story_coverage") != 1.0
                or r.get("material_fact_coverage") != 1.0
                or not isinstance(r.get("utf16_character_count"), int)
                or r["utf16_character_count"] > 4096
            ):
                unmet.append(f"safety_or_coverage:{variant}")
            if r.get("editor_calls", 0) > 2:
                unmet.append("editor_call_budget")
            if r.get("microdetails_preserved") is not True:
                unmet.append(f"microdetails_unverified:{variant}")
    baseline = variants["baseline"]
    candidate = variants["combined"]
    if candidate["failed_or_rejected"] > baseline["failed_or_rejected"]:
        unmet.append("failure_regression")
    if (
        candidate["editorial_median"] is None
        or candidate["editorial_median"] < 85
        or candidate["editorial_min"] < 75
    ):
        unmet.append("editorial_threshold")
    if baseline["editorial_median"] is not None and candidate["editorial_median"] is not None:
        if (
            baseline["editorial_median"] < 85
            and candidate["editorial_median"] < baseline["editorial_median"] + 5
        ):
            unmet.append("insufficient_editorial_improvement")
    if baseline["editorial_median"] is not None and baseline["editorial_median"] >= 85:
        if any(
            r.get("documented_major_defects_resolved") is not True for r in by_variant["combined"]
        ):
            unmet.append("major_defects_unverified")
        for case in CASES:
            if (
                case in medians["baseline"]
                and case in medians["combined"]
                and medians["combined"][case] < medians["baseline"][case]
            ):
                unmet.append(f"editorial_regression:{case}")
    for case in HOLDOUT:
        if case not in medians["combined"] or medians["combined"][case] < max(
            80, medians["baseline"].get(case, 100)
        ):
            unmet.append(f"holdout_regression:{case}")
    if (
        baseline["p95_seconds"] is None
        or candidate["p95_seconds"] is None
        or candidate["p95_seconds"] > baseline["p95_seconds"] * 1.2
    ):
        unmet.append("duration_regression")
    if baseline["cost_total"] is None or candidate["cost_total"] is None:
        unmet.append("cost_unverified")
    elif candidate["cost_total"] > baseline["cost_total"] * 1.2:
        unmet.append("cost_regression")
    return {
        "rollout_recommended": not unmet,
        "unmet_criteria": list(dict.fromkeys(unmet)),
        "variants": variants,
        "per_case_medians": medians,
    }
