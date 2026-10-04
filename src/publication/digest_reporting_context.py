"""Conservative, source-derived context for digest composition and editing."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from src.publication.evidence import PublicationEvidence

# Only the explicit same-journey comparison form is supported. Separate
# destination fares and fragments without a paid leg stay unparsed.
_FARE_COMPARISON = re.compile(
    r"билет\s+на\s+автобус\s+(?P<leg>[^,;\n]+?\s*[-–—]\s*[^,;\n]+?)\s+"
    r"стоит\s+(?P<ordinary>\d[\d ]*\s+рубл(?:ей|я|ь))\s*,\s*"
    r"а\s+на\s+проходящих\s+автобусах\s*\((?P<destinations>[^()\n]+)\)\s*"
    r"[-–—]\s*(?P<passing>\d[\d ]*\s+рубл(?:ей|я|ь))",
    re.IGNORECASE,
)


def fare_comparison_context(texts: Sequence[str]) -> list[dict[str, str]]:
    """Expose only the leg/price roles expressed by this exact source grammar."""
    rows = []
    for text in dict.fromkeys(texts):
        for match in _FARE_COMPARISON.finditer(text):
            rows.append(
                {
                    "source_text": text,
                    "paid_leg": match.group("leg").strip(),
                    "ordinary_fare": match.group("ordinary").strip(),
                    "passing_bus_fare_for_same_leg": match.group("passing").strip(),
                    "passing_bus_final_destinations": match.group("destinations").strip(),
                }
            )
    return rows


def publish_support_metadata(
    evidence: Mapping[str, PublicationEvidence],
) -> dict[str, dict[str, Any]]:
    """Resolve exact PUBLISH aliases without treating each ref as a new source."""
    indexed: dict[str, list[PublicationEvidence]] = {}
    for key, item in evidence.items():
        if item.publication_use != "PUBLISH":
            continue
        aliases = {key, item.evidence_id, item.source_ref} - {"", None}
        if item.fragment_id is not None:
            aliases.add(f"fragment:{item.fragment_id}")
        for alias in aliases:
            rows = indexed.setdefault(str(alias), [])
            if item not in rows:
                rows.append(item)
    output: dict[str, dict[str, Any]] = {}
    for alias, rows in indexed.items():
        metadata: dict[str, Any] = {
            "canonical_evidence_ids": sorted({row.evidence_id for row in rows}),
        }
        reply_parent_contexts = sorted(
            {
                str(row.reply_parent_context_text).strip()
                for row in rows
                if str(row.reply_parent_context_text or "").strip()
            }
        )
        if reply_parent_contexts:
            metadata["reply_parent_context"] = {
                "kind": "background_only_not_citable",
                "texts": reply_parent_contexts,
            }
        for field in (
            "kind",
            "source_role",
            "source_item_id",
            "source_id",
            "source_item_revision_id",
        ):
            values = {getattr(row, field) for row in rows}
            value = next(iter(values)) if len(values) == 1 else None
            metadata["evidence_kind" if field == "kind" else field] = value
        output[alias] = metadata
    return output


def ambiguous_passing_fare(body: str, texts: Sequence[str]) -> bool:
    """Request clarification only for a supplied same-leg fare comparison."""
    from src.publication.article_claims import _stem

    for comparison in fare_comparison_context(texts):
        destination = re.split(r"\s*[-–—]\s*", comparison["paid_leg"])[-1]
        destination_tokens = {_stem(token.casefold()) for token in re.findall(r"\w+", destination)}
        amount = re.match(r"[\d ]+", comparison["passing_bus_fare_for_same_leg"])
        if amount is None:
            continue
        digits = re.sub(r"\s", "", amount.group())
        amount_pattern = r"(?<!\d)" + r"\s*".join(digits) + r"(?!\d)"
        for sentence in re.split(r"[.!?;\n]", body):
            if not re.search(r"проходящ", sentence, re.IGNORECASE) or not re.search(
                amount_pattern, sentence
            ):
                continue
            sentence_tokens = {_stem(token.casefold()) for token in re.findall(r"\w+", sentence)}
            same_leg_reference = re.search(
                r"\b(?:эт\w*|том|тому|тот|того)\s+(?:же\s+)?(?:участ\w*|маршрут\w*)",
                sentence,
                re.IGNORECASE,
            )
            if not same_leg_reference and not destination_tokens.issubset(sentence_tokens):
                return True
    return False


_SOURCE_SWITCH_CLOCK = re.compile(
    r"\b(?:дали|включили|восстановили)\s+(?:в\s+р-не|в\s+районе|около|в)\s*"
    r"(?P<hour>\d{1,2})\s+час(?:а|ов)?\b",
    re.IGNORECASE,
)


def source_switch_clock_context(texts: Sequence[str]) -> list[dict[str, str]]:
    """Parse only explicit switch-time wording; never infer a supply duration."""
    return [
        {
            "source_text": text,
            "hour": match["hour"],
            "time_expression": match[0],
            "role": "time_of_day",
        }
        for text in dict.fromkeys(texts)
        for match in _SOURCE_SWITCH_CLOCK.finditer(text)
        if int(match["hour"]) < 24
    ]


_REPLY_PARENT_ANNOTATION = re.compile(
    r'\s*\(\s*in_reply_to\s*:\s*(?:"(?:\\.|[^"\\])*"|[^)]*)\s*\)',
    re.IGNORECASE | re.DOTALL,
)


def writer_citable_text(text: str) -> str:
    """Remove serialized parent-message context from citable writer material."""
    return _REPLY_PARENT_ANNOTATION.sub(" ", text or "").strip()
