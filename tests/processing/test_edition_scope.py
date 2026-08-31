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


def test_regionwide_totals_without_focus_anchor_are_detected():
    from src.domain.edition_geography import EditionGeographyContext
    from src.processing.edition_scope import broad_region_without_focus_impact

    scope = EditionScopeConfig(
        name="Example City",
        focus_places=("Example City", "Example District"),
        direct_impact_only=True,
        notes=(),
    )
    geo = EditionGeographyContext(
        edition_slug="example",
        edition_name="Example City",
        target_locations=("Example City",),
        district_locations=("Example District",),
        region_name="Example Region",
    )

    assert broad_region_without_focus_impact(
        basis_texts=("Across Example Region, 200 incidents were recorded",),
        scope=scope,
        geo_context=geo,
    )


def test_region_story_with_explicit_focus_consequence_is_not_detected():
    from src.domain.edition_geography import EditionGeographyContext
    from src.processing.edition_scope import broad_region_without_focus_impact

    scope = EditionScopeConfig(
        name="Example City",
        focus_places=("Example City", "Example District"),
        direct_impact_only=True,
        notes=(),
    )
    geo = EditionGeographyContext(
        edition_slug="example",
        edition_name="Example City",
        target_locations=("Example City",),
        district_locations=("Example District",),
        region_name="Example Region",
    )

    assert not broad_region_without_focus_impact(
        basis_texts=(
            "Across Example Region restrictions were introduced; in Example City buses stopped",
        ),
        scope=scope,
        geo_context=geo,
    )
