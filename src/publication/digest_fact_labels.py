"""Short per-call labels for digest fact IDs in model input and output.

The writer and the editor copied long fact IDs (`infrastructure_electricity_3f32…`)
by hand. One wrong character in a twenty-character hash turns a covered fact into
a coverage failure, and the publication fails. The model now sees short labels
(F1, F2, …) and returns them. Python maps them back to the real IDs before any
validation runs. The model never chooses or invents an ID.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

FACT_LABEL_INSTRUCTION = (
    "Fact IDs in this input are short labels such as F1 and F2. Copy each label "
    "exactly into covered_fact_ids. Never invent, change, or combine a label."
)


class FactLabels:
    """A bijection between one call's fact IDs and labels F1..Fn in input order."""

    def __init__(self, fact_ids: Iterable[str]) -> None:
        self._label_by_id: dict[str, str] = {}
        self._id_by_label: dict[str, str] = {}
        for index, fact_id in enumerate(dict.fromkeys(fact_ids), start=1):
            label = f"F{index}"
            self._label_by_id[fact_id] = label
            self._id_by_label[label] = fact_id

    def __bool__(self) -> bool:
        return bool(self._label_by_id)

    def encode(self, value: Any) -> Any:
        """Replace fact IDs in string values with their labels (copies the structure)."""
        return _map_strings(value, self._label_by_id)

    def decode(self, value: Any) -> Any:
        """Replace labels in string values with their fact IDs (copies the structure).

        Only a value that is exactly a label is replaced, so prose that merely
        contains such text is left alone.
        """
        return _map_strings(value, self._id_by_label)

    def encode_text(self, text: str) -> str:
        """Replace fact IDs embedded in free text, such as validation messages."""
        for fact_id in sorted(self._label_by_id, key=len, reverse=True):
            text = text.replace(fact_id, self._label_by_id[fact_id])
        return text


def fact_labels_for_plan(plan: Any) -> FactLabels:
    """Labels for every required fact of a digest plan; empty when there is no plan."""
    blocks = getattr(plan, "blocks", None) or ()
    fact_ids = [
        str(getattr(fact, "fact_id", ""))
        for block in blocks
        for fact in (getattr(block, "required_facts", None) or ())
    ]
    return FactLabels(fact_id for fact_id in fact_ids if fact_id)


def _map_strings(value: Any, mapping: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, Mapping):
        return {key: _map_strings(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_map_strings(item, mapping) for item in value]
    return value
