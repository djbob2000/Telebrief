"""Rollout reports include failure, missing costs and holdout regressions."""

# ruff: noqa: S101


def rows():
    return [
        {
            "case": case,
            "variant": variant,
            "repeat": i,
            "status": "accepted",
            "editorial_scores": [4, 4, 4, 4, 4] if variant == "combined" else [3, 3, 3, 4, 4],
            "elapsed_seconds": 10,
            "cost": 0.1,
            "editor_calls": 1,
            "story_coverage": 1.0,
            "material_fact_coverage": 1.0,
            "utf16_character_count": 1000,
            "microdetails_preserved": True,
            "documented_major_defects_resolved": True,
        }
        for case in (
            "rich_utilities",
            "quiet_day",
            "local_contrasts",
            "practical_services",
            "partial_announcements",
            "community_microdetails",
        )
        for variant in ("baseline", "combined")
        for i in (1, 2, 3)
    ]


def test_failed_generations_remain_in_comparison():
    from scripts.digest_evaluation.reporting import summarize_digest_comparison

    data = rows()
    data[-1]["status"] = "failed"
    data[-1].pop("editorial_scores")
    report = summarize_digest_comparison(data)
    assert report["variants"]["combined"]["failed_or_rejected"] == 1
    assert not report["rollout_recommended"]


def test_missing_cost_is_not_reported_as_zero():
    from scripts.digest_evaluation.reporting import summarize_digest_comparison

    data = rows()
    data[-1]["cost"] = None
    report = summarize_digest_comparison(data)
    assert report["variants"]["combined"]["cost_total"] is None
    assert "cost_unverified" in report["unmet_criteria"]
    assert not report["rollout_recommended"]


def test_holdout_regression_prevents_rollout_recommendation():
    from scripts.digest_evaluation.reporting import summarize_digest_comparison

    data = rows()
    for row in data:
        if row["variant"] == "combined" and row["case"] == "community_microdetails":
            row["editorial_scores"] = [3, 3, 3, 3, 3]
    assert (
        "holdout_regression:community_microdetails"
        in summarize_digest_comparison(data)["unmet_criteria"]
    )


def test_complete_comparison_can_recommend_rollout():
    from scripts.digest_evaluation.reporting import summarize_digest_comparison

    report = summarize_digest_comparison(rows())
    assert report["rollout_recommended"]
    assert report["unmet_criteria"] == []


def test_provider_usage_keeps_missing_cost_unknown():
    from scripts.digest_evaluation.usage import summarize_usage

    assert (
        summarize_usage(
            [
                {"cost": 0.2, "prompt_tokens": 10, "completion_tokens": 5},
                {"cost": None, "prompt_tokens": 20, "completion_tokens": 5},
            ]
        )["cost"]
        is None
    )
    assert (
        summarize_usage([{"cost": 0.0, "prompt_tokens": 10, "completion_tokens": 5}])["cost"] == 0.0
    )


def test_high_baseline_does_not_hide_development_regression():
    from scripts.digest_evaluation.reporting import summarize_digest_comparison

    data = rows()
    for row in data:
        row["editorial_scores"] = [4, 4, 4, 4, 4]
        if row["variant"] == "combined" and row["case"] == "quiet_day":
            row["editorial_scores"] = [3, 3, 4, 4, 4]
    assert "editorial_regression:quiet_day" in summarize_digest_comparison(data)["unmet_criteria"]


def test_high_baseline_requires_documented_major_defects_resolved():
    from scripts.digest_evaluation.reporting import summarize_digest_comparison

    data = rows()
    for row in data:
        row["editorial_scores"] = [4, 4, 4, 4, 4]
        row.pop("documented_major_defects_resolved", None)
    report = summarize_digest_comparison(data)
    assert "major_defects_unverified" in report["unmet_criteria"]


def test_missing_duration_cannot_hide_slow_or_failed_case():
    from scripts.digest_evaluation.reporting import summarize_digest_comparison

    data = rows()
    data[-1].pop("elapsed_seconds")
    report = summarize_digest_comparison(data)
    assert report["variants"]["combined"]["p95_seconds"] is None
    assert "duration_regression" in report["unmet_criteria"]
