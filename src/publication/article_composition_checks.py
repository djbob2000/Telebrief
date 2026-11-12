"""Composition checks and writer rules for article fidelity (W5.3, W7.1, W7.3, W8).

The detector is deterministic and takes plain inputs, so ``article_quality`` can call it
without an import cycle. Its findings are repair-level reader-quality findings; they never
block publication.

Conflicting accounts (W5.3) are visible only from structured ``service_state`` carried on
``ServiceSubjectHint``: the same resolved location, the same ``subject_key``, opposite
states (AVAILABLE against UNAVAILABLE), supported by different source items, and one
paragraph that states them without a disagreement marker. Anything else is not evaluated.
Speakers are told apart by their speaker key; supports without one fall back to source items.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from src.publication.evidence import ServiceSubjectHint

UNFRAMED_CONFLICTING_ACCOUNTS = "UNFRAMED_CONFLICTING_ACCOUNTS"

# A paragraph that already presents the disagreement as one is never a finding.
DISAGREEMENT_MARKER_RE = re.compile(
    r"по[\s-]разному|расходятся|по\s+другим\s+сведениям|тогда\s+как|а\s+по\s+словам|"
    r"однако\s+другой",
    re.IGNORECASE,
)
_OPPOSITE_STATES = frozenset({"AVAILABLE", "UNAVAILABLE"})


@dataclass(frozen=True)
class CompositionFinding:
    code: str
    unit_id: str
    message: str
    support_ids: tuple[str, ...]


@dataclass(frozen=True)
class _StructuredAccount:
    support_id: str
    subject_key: str
    state: str
    location_ids: frozenset[str]
    source_item_ids: frozenset[object]


def _structured_account(
    support_id: str,
    hint: ServiceSubjectHint | None,
    source_item_ids: frozenset[object],
    location_ids: Callable[[str], frozenset[str]],
) -> _StructuredAccount | None:
    if hint is None or hint.state not in _OPPOSITE_STATES or not hint.location.strip():
        return None
    resolved = location_ids(hint.location)
    if not resolved or not source_item_ids:
        return None
    return _StructuredAccount(
        support_id=support_id,
        subject_key=hint.subject_key,
        state=hint.state,
        location_ids=resolved,
        source_item_ids=source_item_ids,
    )


def find_unframed_conflicting_accounts(
    paragraphs: Sequence[tuple[str, str, tuple[str, ...]]],
    *,
    hint_for: Callable[[str], ServiceSubjectHint | None],
    source_items_for: Callable[[str], frozenset[object]],
    location_ids: Callable[[str], frozenset[str]],
) -> tuple[CompositionFinding, ...]:
    """Find one paragraph that states two opposite service accounts without a framing.

    ``paragraphs`` holds (unit_id, prose, citable support ids) in reading order.
    """
    findings: list[CompositionFinding] = []
    for unit_id, text, support_ids in paragraphs:
        if not text or DISAGREEMENT_MARKER_RE.search(text):
            continue
        accounts = [
            account
            for support_id in dict.fromkeys(support_ids)
            if (
                account := _structured_account(
                    support_id,
                    hint_for(support_id),
                    source_items_for(support_id),
                    location_ids,
                )
            )
            is not None
        ]
        conflict: tuple[_StructuredAccount, _StructuredAccount] | None = None
        for index, first in enumerate(accounts):
            for second in accounts[index + 1 :]:
                if (
                    first.subject_key == second.subject_key
                    and first.state != second.state
                    and first.location_ids & second.location_ids
                    and not (first.source_item_ids & second.source_item_ids)
                ):
                    conflict = (first, second)
                    break
            if conflict is not None:
                break
        if conflict is None:
            continue
        findings.append(
            CompositionFinding(
                code=UNFRAMED_CONFLICTING_ACCOUNTS,
                unit_id=unit_id,
                message=(
                    "Абзац излагает два противоположных состояния одной службы в одном месте, "
                    "которые сообщили разные источники, как два утверждения подряд. Подайте это "
                    "как расхождение («жители описывают по-разному: …») или разнесите по разным "
                    "абзацам; сохраните оба подтверждённых сообщения."
                ),
                support_ids=tuple(account.support_id for account in conflict),
            )
        )
    return tuple(findings)


# Writer rules (article-fidelity W5.3, W7.1, W7.3, W8 and the canonical terms of W6.4).
# Included in the article narrative contract through a one-line hook. Examples are generic.
ARTICLE_FIDELITY_WRITER_RULES = """Source fidelity details:
- A service the source does not name stays unnamed. When a sentence says only "у нас не было 9 дней" or similar, name the service from the support's topic hint or from its Gate fact, and never fill the gap with another noun (for example, do not write "дома не было девять дней" for a sentence about power). When neither names the service, write the sentence without a service noun.
- Conflicting accounts: when two speakers describe the same part of an area and the same service differently, present it as a disagreement, for example «жители описывают по-разному: …». Do not place two contradictory statements about the same place and service in consecutive sentences as plain facts.
- Ordinal durations: "4 неделю", "третьи сутки" and "11 день" count a day or week already in progress. Write «идёт четвёртая неделя», never «уже четыре недели».
- Hedges and hearsay stay in the sentence that states the fact: "похоже", "кажется", "вроде", "видимо", "говорят", "по слухам" and attributions such as "со слов соседей". Never drop them and never upgrade them into an established status.
- canonical_terms, when present in a support record, lists a variant and its canonical spelling: write the canonical spelling outside direct quotes. A direct quote keeps its exact wording.
"""
