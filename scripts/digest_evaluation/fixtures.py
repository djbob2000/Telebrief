"""Lossless, explicitly requested JSON snapshots with a closed type registry."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from src import collector, editorial_models
from src.config.schemas import publication as config_models
from src.publication import (
    city_situation,
    digest_composition,
    digest_narrative,
    digest_presentation,
    editorial_adapter,
    evidence,
)
from src.publication.digest_assessment import DigestAssessmentContext
from src.publication.renderers import PublicationDigestRenderer


@dataclass(frozen=True)
class FrozenDigestCase:
    name: str
    context: DigestAssessmentContext
    generation: dict[str, Any]
    implementation_versions: dict[str, str]


_MODULES = (
    collector,
    editorial_models,
    config_models,
    city_situation,
    digest_composition,
    digest_narrative,
    digest_presentation,
    editorial_adapter,
    evidence,
)
_REGISTRY: dict[str, Any] = {
    f"{value.__module__}.{value.__name__}": value
    for module in _MODULES
    for value in vars(module).values()
    if isinstance(value, type) and (is_dataclass(value) or issubclass(value, Enum))
}
_REGISTRY.update(
    {f"{cls.__module__}.{cls.__name__}": cls for cls in (FrozenDigestCase, DigestAssessmentContext)}
)


def _encode(value: Any) -> Any:
    if isinstance(value, Enum):
        return {"$enum": f"{type(value).__module__}.{type(value).__name__}", "value": value.value}
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dt.datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, PublicationDigestRenderer):
        return {
            "$renderer": {
                "output_language": value.output_language,
                "use_emojis": value.use_emojis,
                "include_statistics": value.include_statistics,
                "rubrics_config": _encode(value.rubrics_config),
            },
            "rubrics": _encode(value.rubrics),
        }
    if is_dataclass(value) and not isinstance(value, type):
        tag = f"{type(value).__module__}.{type(value).__name__}"
        if tag not in _REGISTRY:
            raise ValueError("DIGEST_CASE_UNSUPPORTED_TYPE")
        return {
            "$dataclass": tag,
            "fields": {f.name: _encode(getattr(value, f.name)) for f in fields(value)},
        }
    if isinstance(value, tuple):
        return {"$tuple": [_encode(v) for v in value]}
    if isinstance(value, (set, frozenset)):
        return {
            "$frozenset": sorted(
                (_encode(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True)
            )
        }
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {
            "$mapping": [
                [_encode(k), _encode(v)]
                for k, v in sorted(value.items(), key=lambda row: str(row[0]))
            ]
        }
    raise ValueError("DIGEST_CASE_UNSUPPORTED_TYPE")


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if "$datetime" in value:
        return dt.datetime.fromisoformat(value["$datetime"])
    if "$renderer" in value:
        options = {k: _decode(v) for k, v in value["$renderer"].items()}
        renderer = PublicationDigestRenderer(**options)
        renderer.rubrics = _decode(value["rubrics"])
        return renderer
    if "$tuple" in value:
        return tuple(_decode(v) for v in value["$tuple"])
    if "$frozenset" in value:
        return frozenset(_decode(v) for v in value["$frozenset"])
    if "$mapping" in value:
        return {_decode(k): _decode(v) for k, v in value["$mapping"]}
    if "$enum" in value:
        return _REGISTRY[value["$enum"]](value["value"])
    if "$dataclass" in value:
        cls = _REGISTRY[value["$dataclass"]]
        args = {k: _decode(v) for k, v in value["fields"].items()}
        if cls is digest_presentation.DigestPresentationPlan:
            args["city_situation"] = args.pop("_city_situation")
            args["story_presentations"] = args.pop("_story_presentations")
        return cls(**args)
    raise ValueError("DIGEST_CASE_UNKNOWN_ENCODING")


def write_digest_case(case: FrozenDigestCase, path: Path) -> str:
    payload = {"version": "digest-case-v1", "case": _encode(case)}
    text = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    return hashlib.sha256(text.encode()).hexdigest()


def load_digest_case(path: Path) -> FrozenDigestCase:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["version"] != "digest-case-v1":
            raise ValueError("DIGEST_CASE_VERSION")
        case = _decode(data["case"])
        if not isinstance(case, FrozenDigestCase) or not case.name or not case.context.plan.blocks:
            raise ValueError("DIGEST_CASE_INCOMPLETE")
        if case.context.snapshot_at.utcoffset() is None:
            raise ValueError("DIGEST_CASE_TIMEZONE")
        required = {"language", "model", "max_output_tokens", "timeout_seconds"}
        if not required.issubset(case.generation) or not case.implementation_versions:
            raise ValueError("DIGEST_CASE_SETTINGS")
        digest_narrative._composition_writer_payload(
            plan=case.context.plan,
            evidence=case.context.evidence,
            cards=case.context.frozen.analysis.cards,
        )
        return case
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("DIGEST_CASE_INVALID") from exc
