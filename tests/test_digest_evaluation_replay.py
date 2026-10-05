"""Replay has no persistence/delivery and labels unsafe results."""

# ruff: noqa: S101
import asyncio
import json

from digest_evaluation_helpers import assessment_inputs


class Provider:
    def __init__(self, output):
        self.output = output

    async def chat_completion(self, **kwargs):
        return self.output


def make_case():
    from scripts.digest_evaluation.fixtures import FrozenDigestCase
    from src.publication.digest_assessment import DigestAssessmentContext

    values, draft = assessment_inputs()
    raw = {
        "blocks": [
            {
                "block_id": b.block_id,
                "items": [
                    {
                        "composition_unit_ids": list(i.composition_unit_ids),
                        "covered_fact_ids": list(i.covered_fact_ids),
                        "headline": i.headline,
                        "body": i.body,
                        "claims": [],
                    }
                    for i in b.items
                ],
            }
            for b in draft.blocks
        ]
    }
    case = FrozenDigestCase(
        "rich",
        DigestAssessmentContext(**values),
        {"language": "Russian", "model": None, "max_output_tokens": 4096, "timeout_seconds": 120},
        {"writer": "test"},
    )
    return case, raw


def test_replay_never_touches_runtime_database_or_delivery(monkeypatch):
    import src.bootstrap
    import src.runtime
    from scripts.digest_evaluation.replay import replay_digest

    def forbidden(*args, **kwargs):
        raise AssertionError("replay attempted runtime/bootstrap side effects")

    monkeypatch.setattr(src.runtime, "get_runtime", forbidden)
    monkeypatch.setattr(src.bootstrap, "build_infrastructure", forbidden)
    case, raw = make_case()
    result = asyncio.run(replay_digest(case, provider=Provider(json.dumps(raw))))
    assert result.status == "accepted"
    assert result.assessment.coverage.story_coverage == 1.0
    assert result.diagnostics["provider_calls"] == 1
    assert result.diagnostics["cost"] is None


def test_replay_allows_editor_to_restore_a_writer_omitted_story():
    from scripts.digest_evaluation.replay import replay_digest

    case, complete = make_case()
    block = complete["blocks"][0]
    missing_item = block["items"].pop(0)
    incomplete = {"blocks": complete["blocks"]}
    editor = {
        "blocks": [
            {
                "block_id": source_block["block_id"],
                "recomposed_items": [
                    {
                        "composition_unit_ids": item["composition_unit_ids"],
                        "covered_fact_ids": item["covered_fact_ids"],
                        "headline": item["headline"],
                        "body": item["body"],
                        "claims": [],
                    }
                    for item in (
                        [missing_item]
                        + source_block["items"]
                    )
                ],
            }
            for source_block in complete["blocks"]
        ]
    }

    class SequenceProvider:
        def __init__(self):
            self.calls = 0

        async def chat_completion(self, **kwargs):
            self.calls += 1
            return json.dumps(incomplete if self.calls == 1 else editor)

    provider = SequenceProvider()
    case = type(case)(
        case.name,
        case.context,
        {
            **case.generation,
            "editor_scope": "thematic_blocks",
            "review_without_findings": True,
        },
        case.implementation_versions,
    )
    result = asyncio.run(replay_digest(case, provider=provider))
    assert result.status == "accepted"
    assert result.assessment.coverage.story_coverage == 1.0
    assert result.assessment.coverage.material_fact_coverage == 1.0
    assert result.diagnostics["editor_calls"] == 1
    assert provider.calls == 2


def test_rejected_and_failed_outputs_label_checkpoint_status(tmp_path):
    from scripts.digest_evaluation.replay import replay_digest, write_replay_result

    case, raw = make_case()
    raw["blocks"][0]["items"][0]["body"] += " Напряжение 999 В."
    rejected = asyncio.run(replay_digest(case, provider=Provider(json.dumps(raw))))
    assert rejected.status == "rejected"
    path = tmp_path / "rejected"
    write_replay_result(rejected, path)
    assert (path.with_suffix(".txt")).read_text().startswith("REJECTED PREVIEW — DO NOT PUBLISH")
    failed = asyncio.run(
        replay_digest(case, provider=Provider("private secret exception source prose"))
    )
    assert failed.status == "failed"
    write_replay_result(failed, tmp_path / "failed")
    assert "private secret" not in (tmp_path / "failed.json").read_text()
    assert "No assessed draft" in (tmp_path / "failed.txt").read_text()


def test_replay_refuses_changed_geographic_profile_before_provider():
    from dataclasses import replace

    from scripts.digest_evaluation.replay import replay_digest

    case, _ = make_case()
    case = replace(
        case,
        context=replace(case.context, plan=replace(case.context.plan, edition_slug="berdyansk")),
        generation={**case.generation, "geography_profile_hash": "wrong"},
    )

    class Provider:
        async def chat_completion(self, **kwargs):
            raise AssertionError("changed profile reached provider")

    result = asyncio.run(replay_digest(case, provider=Provider()))
    assert result.status == "failed"
    assert result.diagnostics["provider_calls"] == 0
    assert result.diagnostics["error_kind"] == "DigestReplayDependencyError"


def test_replay_accepts_unchanged_frozen_geographic_profile():
    import hashlib
    from dataclasses import replace
    from pathlib import Path

    from scripts.digest_evaluation.replay import replay_digest

    case, raw = make_case()
    case = replace(
        case,
        context=replace(case.context, plan=replace(case.context.plan, edition_slug="berdyansk")),
        generation={
            **case.generation,
            "geography_profile_hash": hashlib.sha256(
                Path("data/city_profiles/berdyansk.yaml").read_bytes()
            ).hexdigest(),
        },
    )
    result = asyncio.run(replay_digest(case, provider=Provider(json.dumps(raw))))
    assert result.status == "accepted"
    assert result.diagnostics["provider_calls"] == 1
