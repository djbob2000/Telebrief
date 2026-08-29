from src.config_loader import EditionScopeConfig
from src.processing.edition_scope import build_scope_contract, scope_config_hash


def test_scope_hash_changes_when_focus_area_changes():
    first = EditionScopeConfig(name="City", focus_places=("City",))
    second = EditionScopeConfig(name="City", focus_places=("City", "District"))
    assert scope_config_hash(first) != scope_config_hash(second)


def test_scope_contract_names_all_scope_classes():
    scope = EditionScopeConfig(name="City", focus_places=("City",))
    contract = build_scope_contract(scope)
    assert "LOCAL" in contract
    assert "DIRECT_IMPACT" in contract
    assert "OUT_OF_SCOPE" in contract
    assert "UNCERTAIN" in contract


def test_scope_contract_rejects_source_membership_as_locality_evidence():
    scope = EditionScopeConfig(name="City", focus_places=("City",))
    contract = build_scope_contract(scope).lower()
    assert "source membership alone" in contract
    assert "same region" in contract
