"""Gate prompt rules: ordinal durations stay ordinal and hedges stay in the fact (W7.1, W7.3)."""

# ruff: noqa: S101
from src.processing.event_triage import _GATE_V2_SYSTEM_PROMPT as GATE_PROMPT
from src.processing.gate_fidelity_rules import GATE_ORDINAL_AND_HEDGE_RULES


def test_rules_are_rendered_into_the_gate_system_prompt():
    assert GATE_ORDINAL_AND_HEDGE_RULES in GATE_PROMPT


def test_ordinal_duration_rule_names_the_real_case_forms():
    rule = GATE_ORDINAL_AND_HEDGE_RULES
    assert "4 неделю" in rule
    assert "третьи сутки" in rule
    assert "11 день" in rule
    assert "идёт четвёртая неделя" in rule
    assert "Never turn an ordinal into a cardinal total" in rule
    assert "уже четыре недели" in rule  # the forbidden rewrite is named explicitly


def test_hedge_rule_lists_the_hedge_and_hearsay_markers():
    rule = GATE_ORDINAL_AND_HEDGE_RULES
    for marker in (
        "похоже",
        "кажется",
        "вроде",
        "видимо",
        "говорят",
        "по слухам",
        "со слов соседей",
    ):
        assert marker in rule, marker
    assert "Never drop them" in rule


def test_ordinal_and_hedge_rules_sit_before_the_chat_sarcasm_section():
    assert GATE_PROMPT.index("ORDINAL DURATIONS AND HEDGES") < GATE_PROMPT.index(
        "CHAT SARCASM, RUMORS, AND SCHEDULED OUTAGES"
    )


def test_examples_are_generic_not_edition_specific():
    """AGENTS.md §0.9: no city examples in generic production prompt logic."""
    rule = GATE_ORDINAL_AND_HEDGE_RULES.casefold()
    for local_term in ("бердянск", "колони", "гора", "частн", "азмол", "акз"):
        assert local_term not in rule, local_term
