from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from src.city_context import CityContextResolver, CityProfileError, StoryContextEnricher
from src.city_context_models import (
    AreaCandidate,
    AreaEvidence,
    CityContextAnnotation,
    ResolvedEntity,
    ScaleEvidence,
    StoryContext,
)
from src.collector import Message
from src.editorial_models import (
    EditorialAnalysis,
    PreparedBundle,
    SourceRecord,
    StoryCard,
    StoryElement,
)


def test_city_profile_yaml_structure():
    path = Path("data/city_profiles/berdyansk.yaml")
    with open(path, "r", encoding="utf-8") as f:
        profile = yaml.safe_load(f)

    assert profile["schema_version"] == 2
    assert profile["profile_id"] == "berdyansk"
    assert profile["safety_contract"]["context_not_event_evidence"] is True
    assert (
        profile["stable_context"]["geography"]["editorial_scale_area_set"]["exhaustive_for_scale"]
        is False
    )
    assert len(profile["stable_context"]["geography"]["street_gazetteer"]["entries"]) >= 400


def test_city_profile_yaml_coverage_conversion():
    path = Path("data/city_profiles/berdyansk.yaml")
    with open(path, "r", encoding="utf-8") as f:
        profile = yaml.safe_load(f)

    entries = profile["stable_context"]["geography"]["street_gazetteer"]["entries"]
    rule_count = 0
    coverage_count = 0
    for entry in entries:
        for m in entry.get("area_memberships", []):
            if "rule" in m:
                rule_count += 1
            if "coverage" in m:
                coverage_count += 1

    assert rule_count == 0
    assert coverage_count == 468


def test_city_context_models_instantiation():
    cand = AreaCandidate(
        area_set="municipal",
        area_id="center",
        area_name="Центр",
        confidence="high",
        coverage_kind="whole_object",
        source_ref="gazetteer",
    )
    entity = ResolvedEntity(
        kind="place",
        entity_id="street:Шевченка",
        matched_text="Шевченко",
        canonical_name="вулиця Шевченка",
        object_type="street",
        municipal_areas=(cand,),
    )
    annot = CityContextAnnotation(entities=(entity,))
    assert len(annot.entities) == 1
    assert annot.entities[0].entity_id == "street:Шевченка"

    evidence = AreaEvidence(
        area_set="municipal_neighborhood_committees_2021",
        area_id="center",
        source_refs=("S000001", "S000002"),
        direct_area_refs=("S000002",),
        inferred_from_place_refs=("S000001",),
    )
    assert evidence.area_set == "municipal_neighborhood_committees_2021"
    assert evidence.area_id == "center"
    assert len(evidence.source_refs) == 2

    scale = ScaleEvidence(
        observed_area_ids=("center", "liski"),
        observed_count=2,
        geographic_spread=True,
        broad_prevalence_supported=False,
        majority_supported=False,
    )
    context = StoryContext(
        card_id="SC001",
        municipal_areas=(evidence,),
        scale=scale,
    )
    assert context.card_id == "SC001"
    assert context.scale.geographic_spread is True


def test_city_context_resolver_exact_and_aliases():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")

    # Direct area mentions (stable identity + canonical name)
    res_center = resolver.resolve("В центре снова нет света")
    area_center = next(
        e for e in res_center.entities if e.kind == "area" and e.entity_id == "center"
    )
    assert area_center.entity_id == "center"
    assert area_center.canonical_name == "Центр міста"

    res_liski = resolver.resolve("На Лисках нет воды")
    area_liski = next(e for e in res_liski.entities if e.kind == "area" and e.entity_id == "liski")
    assert area_liski.entity_id == "liski"
    assert area_liski.canonical_name == "Ліски"

    # Streets and aliases
    res1 = resolver.resolve("На ул. Шевченко нет света")
    assert any(e.entity_id == "street:Шевченка" for e in res1.entities)

    res2 = resolver.resolve("На бульваре Шевченко нет света")
    assert any(e.entity_id == "boulevard:Шевченка" for e in res2.entities)

    res3 = resolver.resolve("На Морозова воды нет")
    assert any(e.entity_id == "street:Північна" for e in res3.entities)

    # Providers and routes
    res4 = resolver.resolve("Юпитер снова работает")
    assert any(e.entity_id == "jupiter" for e in res4.entities)

    res5 = resolver.resolve("Поинт лежит")
    assert any(e.entity_id == "point" for e in res5.entities)

    res6 = resolver.resolve("маршрут 15 сегодня не ходит")
    assert any(e.entity_id == "route:15" for e in res6.entities)

    # Bare number outside transport context does not become route 15
    res7 = resolver.resolve("В доме 15 отключили воду")
    assert not any(e.entity_id == "route:15" for e in res7.entities)


def test_city_context_resolver_multi_area_ambiguity():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")

    res = resolver.resolve("На Мелитопольском шоссе нет света")
    street_entity = next(
        e
        for e in res.entities
        if "Мелітопольське" in e.canonical_name or "шосе" in e.canonical_name
    )
    assert len(street_entity.municipal_areas) > 1
    assert street_entity.confidence == "ambiguous"


def test_city_context_resolver_coverage_narrowing_and_precedence():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")

    # 1. Side / Parity narrowing on вул. Космонавтів (odd -> AKZ, even -> Sklovolokno)
    res_odd = resolver.resolve("вул. Космонавтів 15")
    entity_odd = next(e for e in res_odd.entities if e.entity_id == "street:Космонавтів")
    assert len(entity_odd.municipal_areas) == 1
    assert entity_odd.municipal_areas[0].area_id == "akz"
    assert entity_odd.confidence == "high"

    res_even = resolver.resolve("вул. Космонавтів 16")
    entity_even = next(e for e in res_even.entities if e.entity_id == "street:Космонавтів")
    assert len(entity_even.municipal_areas) == 1
    assert entity_even.municipal_areas[0].area_id == "sklovolokno"
    assert entity_even.confidence == "high"

    # 2. Specific house in list on вул. Волонтерів (158 -> nahirna)
    res_vol = resolver.resolve("вул. Волонтерів 158")
    entity_vol = next(e for e in res_vol.entities if e.entity_id == "street:Волонтерів")
    assert len(entity_vol.municipal_areas) == 1
    assert entity_vol.municipal_areas[0].area_id == "nahirna"
    assert entity_vol.confidence == "high"

    # 3. Numeric range on вул. Мічуріна (105 in 99-117 -> slobidka)
    res_mich_105 = resolver.resolve("вул. Мічуріна 105")
    entity_mich_105 = next(e for e in res_mich_105.entities if e.entity_id == "street:Мічуріна")
    assert len(entity_mich_105.municipal_areas) == 1
    assert entity_mich_105.municipal_areas[0].area_id == "slobidka"
    assert entity_mich_105.confidence == "high"

    # 4. Definite NO_MATCH on specific rule narrows to baseline whole_object:
    # On Мічуріна: Slobidka is house_numbers (99-117, 228-230/6). House 50 is NO_MATCH for Slobidka.
    # Koloniia-Makorty is whole_object MATCH. Result: Koloniia-Makorty with high confidence.
    res_mich_50 = resolver.resolve("вул. Мічуріна 50")
    entity_mich_50 = next(e for e in res_mich_50.entities if e.entity_id == "street:Мічуріна")
    assert len(entity_mich_50.municipal_areas) == 1
    assert entity_mich_50.municipal_areas[0].area_id == "koloniia_makorty"
    assert entity_mich_50.confidence == "high"

    # 5. Missing address on multi-membership street: remains ambiguous (never guess)
    res_mich_bare = resolver.resolve("вул. Мічуріна")
    entity_mich_bare = next(e for e in res_mich_bare.entities if e.entity_id == "street:Мічуріна")
    assert len(entity_mich_bare.municipal_areas) > 1
    assert entity_mich_bare.confidence == "ambiguous"

    # 6. House suffix normalization and segment from_houses on Мелітопольське шосе 106А (Cyrillic А -> RTS)
    res_mel_106a = resolver.resolve("Мелітопольське шосе 106А")
    entity_mel_106a = next(
        e for e in res_mel_106a.entities if e.entity_id == "highway:Мелітопольське шосе"
    )
    assert len(entity_mel_106a.municipal_areas) == 1
    assert entity_mel_106a.municipal_areas[0].area_id == "rts"
    assert entity_mel_106a.confidence == "high"

    # 7. Endpoint/landmark boundary alone is UNKNOWN, not segment match: remains ambiguous
    res_dovg = resolver.resolve("вул. Володимира Довганюка на перехресті з Софіївською")
    entity_dovg = next(e for e in res_dovg.entities if e.entity_id == "street:Володимира Довганюка")
    assert len(entity_dovg.municipal_areas) > 1
    assert entity_dovg.confidence == "ambiguous"


def test_city_context_resolver_cross_entity_ambiguity_and_enricher():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")
    enricher = StoryContextEnricher(resolver)

    # 1. Bare "Шевченко" without object type matches both street and boulevard at same span -> ambiguous
    res_bare = resolver.resolve("На Шевченко нет света")
    places = [e for e in res_bare.entities if e.kind == "place"]
    assert len(places) >= 2
    assert all(p.confidence == "ambiguous" for p in places)

    # 2. Enricher on bare "Шевченко" message does NOT produce false geographic spread
    rec = SourceRecord(
        ref="S000001",
        message=_message("На Шевченко нет света", 1),
        source_type="community",
        city_context=res_bare,
    )
    card = StoryCard(
        id="SC001",
        topic="Отключения света",
        importance="high",
        summary="Отключения света",
        representative_source_refs=["S000001"],
    )
    analysis = EditorialAnalysis(cards=[card])
    bundle = PreparedBundle(
        records={"S000001": rec},
        prompt_text="",
        total_messages=1,
        candidate_count=1,
    )
    ctx = enricher.enrich(analysis, bundle)["SC001"]
    assert ctx.scale.observed_count == 0
    assert ctx.scale.geographic_spread is False

    # 3. Explicit object types across distinct mentions -> high confidence
    res_typed = resolver.resolve("На вул. Шевченка и бульваре Шевченка")
    st = next(e for e in res_typed.entities if e.entity_id == "street:Шевченка")
    bv = next(e for e in res_typed.entities if e.entity_id == "boulevard:Шевченка")
    assert st.confidence == "high"
    assert bv.confidence == "high"

    # 4. Route number in proximity does not become street house number
    res_route_street = resolver.resolve("Маршрут 15 зупинився на вул. Космонавтів")
    assert any(
        e.entity_id == "route:15" and e.confidence == "high" for e in res_route_street.entities
    )
    st_kosm = next(e for e in res_route_street.entities if e.entity_id == "street:Космонавтів")
    # Because 15 was consumed as route number, street Космонавтів has no house number and remains ambiguous
    assert st_kosm.confidence == "ambiguous"


def test_city_context_resolver_composite_routes_and_provider_boundary():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")

    # Composite route numbers
    res_r1 = resolver.resolve("маршрут 4/15 задерживается")
    assert any(e.entity_id == "route:4/15" or "4/15" in e.matched_text for e in res_r1.entities)

    res_r2 = resolver.resolve("автобус 17/15 не пришел")
    assert any("17/15" in e.matched_text for e in res_r2.entities)

    res_r3 = resolver.resolve("маршрутка 5/2 на линии")
    assert any("5/2" in e.matched_text for e in res_r3.entities)

    # Provider boundary matching with + symbol
    res_p1 = resolver.resolve("у клиентов +7Телеком проблемы с интернетом")
    assert any(e.entity_id == "plus7telecom" for e in res_p1.entities)

    res_p2 = resolver.resolve("у клиентов +7 Telecom проблемы")
    assert any(e.entity_id == "plus7telecom" for e in res_p2.entities)

    res_p3 = resolver.resolve("у клиентов +7 Телеком проблемы")
    assert any(e.entity_id == "plus7telecom" for e in res_p3.entities)


def test_city_context_resolver_loader_error_contract(tmp_path):
    # Non-existent file propagates FileNotFoundError
    with pytest.raises(FileNotFoundError):
        CityContextResolver.from_yaml(tmp_path / "non_existent.yaml")

    # Corrupt YAML raises CityProfileError
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("schema_version: 999\nprofile_id: [invalid", encoding="utf-8")
    with pytest.raises(CityProfileError):
        CityContextResolver.from_yaml(bad_yaml)


def _message(text: str, message_id: int) -> Message:
    return Message(
        text=text,
        sender="u",
        timestamp=datetime.now(timezone.utc),
        link="",
        channel_name="ch1",
        has_media=False,
        media_type="",
        message_id=message_id,
    )


def test_story_context_enricher_aggregates_complete_refs_and_sets_spread():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")
    enricher = StoryContextEnricher(resolver)

    rec1 = SourceRecord(
        ref="S000001",
        message=_message("В центре снова нет света", 1),
        source_type="community",
        city_context=resolver.resolve("В центре снова нет света"),
    )
    rec2 = SourceRecord(
        ref="S000002",
        message=_message("На Лисках тоже темно", 2),
        source_type="community",
        city_context=resolver.resolve("На Лисках тоже темно"),
    )

    card = StoryCard(
        id="SC001",
        topic="Отключения света",
        importance="high",
        summary="Отключения зафиксированы в двух районах",
        representative_source_refs=["S000001", "S000002"],
        community_observations=[
            StoryElement(
                text="В центре снова нет света",
                source_refs=["S000001"],
                status="attributed",
            ),
            StoryElement(
                text="На Лисках тоже темно",
                source_refs=["S000002"],
                status="attributed",
            ),
        ],
    )
    analysis = EditorialAnalysis(cards=[card])
    bundle = PreparedBundle(
        records={"S000001": rec1, "S000002": rec2},
        prompt_text="",
        total_messages=2,
        candidate_count=2,
    )

    story_contexts = enricher.enrich(analysis, bundle)
    ctx = story_contexts["SC001"]

    assert len(ctx.municipal_areas) == 2
    assert ctx.scale.observed_count == 2
    assert ctx.scale.geographic_spread is True
    assert ctx.scale.broad_prevalence_supported is False
    assert ctx.scale.majority_supported is False


def test_story_context_enricher_ambiguity_safety():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")
    enricher = StoryContextEnricher(resolver)

    # Melitopolskoe highway without house number spans multiple municipal committees -> ambiguous
    rec1 = SourceRecord(
        ref="S000001",
        message=_message("На Мелитопольском шоссе нет света", 1),
        source_type="community",
        city_context=resolver.resolve("На Мелитопольском шоссе нет света"),
    )

    card = StoryCard(
        id="SC001",
        topic="Отключения света",
        importance="high",
        summary="Отключения на шоссе",
        representative_source_refs=["S000001"],
    )
    analysis = EditorialAnalysis(cards=[card])
    bundle = PreparedBundle(
        records={"S000001": rec1},
        prompt_text="",
        total_messages=1,
        candidate_count=1,
    )

    story_contexts = enricher.enrich(analysis, bundle)
    ctx = story_contexts["SC001"]

    # Ambiguous candidates must NOT enter observed_count or scale
    assert ctx.scale.observed_count == 0
    assert ctx.scale.geographic_spread is False


def test_story_context_enricher_counts_all_card_source_refs():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")
    enricher = StoryContextEnricher(resolver)

    rec1 = SourceRecord(
        ref="S000001",
        message=_message("В центре снова нет света", 1),
        source_type="community",
        city_context=resolver.resolve("В центре снова нет света"),
    )
    rec2 = SourceRecord(
        ref="S000002",
        message=_message("На Лисках тоже темно", 2),
        source_type="community",
        city_context=resolver.resolve("На Лисках тоже темно"),
    )

    # Only S000001 is in representative_source_refs, but S000002 is in community_observations
    card = StoryCard(
        id="SC001",
        topic="Отключения света",
        importance="high",
        summary="Отключения света",
        representative_source_refs=["S000001"],
        community_observations=[
            StoryElement(
                text="На Лисках тоже темно",
                source_refs=["S000002"],
                status="attributed",
            )
        ],
    )
    analysis = EditorialAnalysis(cards=[card])
    bundle = PreparedBundle(
        records={"S000001": rec1, "S000002": rec2},
        prompt_text="",
        total_messages=2,
        candidate_count=2,
    )

    story_contexts = enricher.enrich(analysis, bundle)
    ctx = story_contexts["SC001"]

    assert ctx.scale.observed_count == 2
    assert ctx.scale.geographic_spread is True


def test_story_context_enricher_same_area_deduplication():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")
    enricher = StoryContextEnricher(resolver)

    # S000001 on Shevchenko St (inferred Center), S000002 directly in Center, S000003 in Liski
    rec1 = SourceRecord(
        ref="S000001",
        message=_message("На ул. Шевченко нет света", 1),
        source_type="community",
        city_context=resolver.resolve("На ул. Шевченко нет света"),
    )
    rec2 = SourceRecord(
        ref="S000002",
        message=_message("В центре нет света", 2),
        source_type="community",
        city_context=resolver.resolve("В центре нет света"),
    )
    rec3 = SourceRecord(
        ref="S000003",
        message=_message("На Лисках нет света", 3),
        source_type="community",
        city_context=resolver.resolve("На Лисках нет света"),
    )
    bundle = PreparedBundle(
        records={"S000001": rec1, "S000002": rec2, "S000003": rec3},
        prompt_text="",
        total_messages=3,
        candidate_count=3,
    )
    card = StoryCard(
        id="SC001",
        topic="Отключения света",
        importance="high",
        summary="Отключения света",
        representative_source_refs=["S000001", "S000002", "S000003"],
    )
    analysis = EditorialAnalysis(cards=[card])

    story_contexts = enricher.enrich(analysis, bundle)
    ctx = story_contexts["SC001"]

    # 3 messages from 2 unique areas (Center and Liski)
    assert len(ctx.municipal_areas) == 2
    center_area = next(a for a in ctx.municipal_areas if a.area_id == "center")
    assert center_area.source_refs == ("S000001", "S000002")
    assert center_area.direct_area_refs == ("S000002",)
    assert center_area.inferred_from_place_refs == ("S000001",)
    assert ctx.scale.observed_count == 2
    assert ctx.scale.geographic_spread is True


def test_coverage_evaluator_except_segment_semantics():
    from src.city_context import AddressContext, EvalResult, evaluate_coverage

    # Rule: segment on odd side from house 1 to 77, EXCEPT landmark segment Sofiivska to Sevastopolska
    coverage = {
        "kind": "segment",
        "side": "odd",
        "from_house": 1,
        "to_house": 77,
        "except_segment": {
            "side": "odd",
            "from_landmark": "Sofiivska",
            "to_landmark": "Sevastopolska",
        },
    }

    # 1. Address within 1-77, odd side, outside the landmark exclusion (e.g. house 5) -> MATCH
    ctx_outside = AddressContext(
        house_number="5",
        normalized_house="5",
        house_int=5,
        parity="odd",
        explicit_side=None,
        landmark_segment=None,
    )
    assert evaluate_coverage(coverage, ctx_outside) == EvalResult.MATCH

    # 2. Address within the landmark exclusion -> NO_MATCH
    ctx_inside = AddressContext(
        house_number="15",
        normalized_house="15",
        house_int=15,
        parity="odd",
        explicit_side=None,
        landmark_segment=("софіївської", "севастопольської"),
    )
    assert evaluate_coverage(coverage, ctx_inside) == EvalResult.NO_MATCH

    # 3. Address without side or house evidence -> UNKNOWN
    ctx_unknown = AddressContext(
        house_number=None,
        normalized_house=None,
        house_int=None,
        parity=None,
        explicit_side=None,
        landmark_segment=None,
    )
    assert evaluate_coverage(coverage, ctx_unknown) == EvalResult.UNKNOWN


def test_landmark_based_segment_positive_resolution():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")

    # "вул. Володимира Довганюка від Софіївської до Севастопольської" matches nahirna
    res = resolver.resolve("вул. Володимира Довганюка від Софіївської до Севастопольської")
    entity = next((e for e in res.entities if "Довганюка" in e.canonical_name), None)
    assert entity is not None
    assert entity.confidence == "high"
    assert any(a.area_id == "nahirna" for a in entity.municipal_areas)


def test_untyped_place_matching_does_not_fuzzy_stem_bare_mentions():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")

    # "выставка Айвазовского" does NOT mention a street/boulevard and should not match street:Айвазовського
    res_bare = resolver.resolve("В художественном музее открылась выставка Айвазовского")
    assert not any(e.entity_id == "street:Айвазовського" for e in res_bare.entities)

    # "ул. Айвазовского" explicitly names the street
    res_typed = resolver.resolve("На ул. Айвазовского починили водопровод")
    assert any(e.entity_id == "street:Айвазовського" for e in res_typed.entities)


def test_house_numbers_private_sector_union_semantics():
    from src.city_context import AddressContext, EvalResult, evaluate_coverage

    coverage = {
        "kind": "house_numbers",
        "houses": ["1", "3", "5"],
        "private_sector": True,
    }

    # "частный сектор, дом 29" -> house 29 is not in [1, 3, 5], but is_private_sector is True -> MATCH
    ctx = AddressContext(
        house_number="29",
        normalized_house="29",
        house_int=29,
        parity="odd",
        explicit_side=None,
        landmark_segment=None,
        is_private_sector=True,
    )
    assert evaluate_coverage(coverage, ctx) == EvalResult.MATCH


def test_coverage_evaluator_except_only_rule_semantics():
    from src.city_context import AddressContext, EvalResult, evaluate_coverage

    # Rule without base constraints: kind=segment + except_segment (even side from Pershotravneva to Liepaiska)
    coverage = {
        "kind": "segment",
        "except_segment": {
            "side": "even",
            "from_landmark": "Pershotravneva",
            "to_landmark": "Liepaiska",
        },
    }

    # 1. Address inside excluded segment -> NO_MATCH
    ctx_inside = AddressContext(
        house_number="12",
        normalized_house="12",
        house_int=12,
        parity="even",
        explicit_side=None,
        landmark_segment=("першотравневої", "лієпайської"),
    )
    assert evaluate_coverage(coverage, ctx_inside) == EvalResult.NO_MATCH

    # 2. Address proven outside excluded segment (e.g. odd side or different landmark segment) -> MATCH
    ctx_outside_side = AddressContext(
        house_number="15",
        normalized_house="15",
        house_int=15,
        parity="odd",
        explicit_side=None,
        landmark_segment=None,
    )
    assert evaluate_coverage(coverage, ctx_outside_side) == EvalResult.MATCH

    # 3. Address with no side or landmark evidence -> UNKNOWN
    ctx_unknown = AddressContext(
        house_number=None,
        normalized_house=None,
        house_int=None,
        parity=None,
        explicit_side=None,
        landmark_segment=None,
    )
    assert evaluate_coverage(coverage, ctx_unknown) == EvalResult.UNKNOWN


def test_landmark_start_and_end_boundaries():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")

    # "вул. Будівельна від початку вулиці до Космонавтів" matches start -> Kosmonavtiv (akz)
    res1 = resolver.resolve("вул. Будівельна від початку вулиці до Космонавтів")
    budivelna = next((e for e in res1.entities if "Будівельна" in e.canonical_name), None)
    assert budivelna is not None
    assert budivelna.confidence == "high"
    assert any(a.area_id == "akz" for a in budivelna.municipal_areas)

    # "Східний проспект від Петровського шляху до кінця" matches Petrovskyi_shliakh -> end (center)
    res2 = resolver.resolve("Східний проспект від Петровського шляху до кінця")
    skhidnyi = next((e for e in res2.entities if "Східний" in e.canonical_name), None)
    assert skhidnyi is not None
    assert skhidnyi.confidence == "high"
    assert any(a.area_id == "center" for a in skhidnyi.municipal_areas)


def test_different_landmark_interval_is_unknown_without_topology():
    from src.city_context import AddressContext, EvalResult, evaluate_coverage

    coverage = {
        "kind": "segment",
        "from_landmark": "Pershotravneva",
        "to_landmark": "Liepaiska",
    }
    ctx = AddressContext(
        house_number=None,
        normalized_house=None,
        house_int=None,
        parity=None,
        explicit_side=None,
        landmark_segment=("морської", "лієпайської"),
    )
    assert evaluate_coverage(coverage, ctx) == EvalResult.UNKNOWN


def test_city_context_resolves_proletarsky_genitive_as_prospect_not_area():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")

    result = resolver.resolve("На Пролетарского в нижней части города вода пошла слабым напором")

    prospect = next(entity for entity in result.entities if entity.entity_id == "prospect:Східний")

    assert prospect.object_type == "prospect"
    assert not any(
        entity.kind == "area" and "пролетар" in entity.canonical_name.casefold()
        for entity in result.entities
    )
