from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from tsr.contracts import (BooleanValue, Delivery, EvaluationPolicy, Fact, InputRevision,
    Money, MoneyValue, QuantityValue, VersionRef)
from tsr.domain.matching import compare_offer
from tsr.domain.pricing import quote_purchase, validate_money_input
from tsr.domain.routes import build_route_context, evaluate_route
from tsr.operations.releases import load_demo_release

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)


def input_for(release, **changes):
    base = dict(input_revision_id=uuid4(), owner_id=uuid4(), case_id=uuid4(), created_at=NOW,
        previous_id=None, category_id=release.profile.category_id,
        profile_ref=VersionRef(id=release.profile.profile_id, version=release.profile.version),
        region_code='ru-alt', role='self', prescribed={
            'seat_width': QuantityValue(value='420', unit='mm'),
            'max_user_mass': QuantityValue(value='100', unit='kg'),
            'foldable': BooleanValue(value=True)}, unspecified_fields=(),
        certificate_amount=Money(minor=10000000), certificate_applicable_declared='yes',
        route_answers={'applicant_status_declared': 'yes'}, document_fields={}, confirmations=())
    base.update(changes)
    return InputRevision(**base)


def known(value, offer):
    return Fact(status='known', value=value, evidence=offer.price.evidence)


def test_synthetic_matching_known_mismatch_and_unknown():
    release = load_demo_release(ROOT)
    inp = input_for(release)
    classes = []
    for offer in release.offers[:3]:
        result = compare_offer(uuid4(), inp, offer, release.profile,
                               VersionRef(id='matching-v1', version='1.0.0'), NOW)
        assert result.ok, result.error
        classes.append(result.value.classification)
    assert classes == ['complete', 'mismatch', 'incomplete']
    assert compare_offer(uuid4(), input_for(release, prescribed={}), release.offers[0],
        release.profile, VersionRef(id='matching-v1', version='1.0.0'), NOW).value.classification == 'incomplete'


def test_money_input_precision_and_business_limit():
    assert validate_money_input('100000000,00', 'ru').value.minor == 10000000000
    for raw in ('-1', 'NaN', 'Infinity', '1.001', '100000000.01'):
        assert not validate_money_input(raw, 'ru').ok
    assert validate_money_input('0', 'ru').value.minor == 0


def test_negative_certificate_precedence_unknown_shipping_and_large_total():
    release = load_demo_release(ROOT)
    offer = release.offers[0]
    alg = VersionRef(id='pricing-v1', version='1.0.0')
    base = quote_purchase(input_for(release), offer, alg).value
    assert (base.coverage.minor, base.gap.minor, base.possible_own_total.minor) == (10000000, 2000000, 2000000)
    negative = quote_purchase(input_for(release, certificate_applicable_declared='no', certificate_amount=None), offer, alg).value
    assert negative.certificate_use == 'not_applicable'
    assert (negative.coverage.minor, negative.gap.minor) == (0, 12000000)
    unknown = quote_purchase(input_for(release), release.offers[2], alg).value
    assert unknown.possible_own_total is None
    large = offer.model_copy(update={'price':known(MoneyValue(value=Money(minor=10000000000)),offer),
        'delivery':Delivery(mode='separate', charge=known(MoneyValue(value=Money(minor=10000000000)),offer), terms=offer.delivery.terms)})
    result = quote_purchase(input_for(release, certificate_applicable_declared='no'), large, alg)
    assert result.ok
    assert result.value.possible_own_total.minor == 20000000000
    assert result.value.assumptions
    assert quote_purchase(input_for(release), release.offers[3], alg).value.price is None


def test_route_four_statuses_and_draft_pilot_block():
    release = load_demo_release(ROOT)
    policy = EvaluationPolicy(case_mode='demo', allow_synthetic_draft=True)
    def evaluate(inp, offer=None, policy=policy):
        return evaluate_route(build_route_context(inp, offer or release.offers[0],policy,NOW), release.route,release.route_lifecycle,NOW)
    assert evaluate(input_for(release)).value.status == 'preliminary_match'
    assert evaluate(input_for(release, region_code='ru-mow')).value.status == 'not_covered'
    assert evaluate(input_for(release, region_code=None)).value.status == 'needs_clarification'
    assert evaluate(input_for(release,route_answers={})).value.status == 'needs_clarification'
    assert evaluate(input_for(release,route_answers={'applicant_status_declared':'no'})).value.status == 'blocked'
    result = evaluate(input_for(release),policy=EvaluationPolicy(case_mode='pilot',allow_synthetic_draft=True))
    assert not result.ok and result.error.code == 'DATA_NOT_READY'


def test_conflict_optional_unknown_and_independent_supplier_conditions():
    release=load_demo_release(ROOT)
    offer=release.offers[0]
    from tsr.contracts import LifecycleRecord
    unknown=Fact.unknown('not_confirmed')
    changed=offer.model_copy(update={'attributes':dict(offer.attributes,foldable=unknown)})
    result=compare_offer(uuid4(),input_for(release),changed,release.profile,VersionRef(id='matching',version='1.0.0'),NOW)
    assert result.value.classification=='complete'
    altered=changed.model_copy(update={'has_fund_contract':unknown})
    policy=EvaluationPolicy(case_mode='demo',allow_synthetic_draft=True)
    context=build_route_context(input_for(release),altered,policy,NOW)
    result=evaluate_route(context,release.route,release.route_lifecycle,NOW)
    assert result.value.status=='needs_clarification'
    by_id={item.condition_id:item.status for item in result.value.conditions}
    assert by_id['certificate_acceptance']=='met' and by_id['fund_contract']=='unknown'
    revoked=LifecycleRecord(ref=release.route_lifecycle.ref,status='revoked',changed_at=NOW)
    assert evaluate_route(context,release.route,revoked,NOW).error.code=='DATA_REVOKED'
    conflict=Fact(status='conflicting',reason_code='source_disagreement',alternatives=(
        known(QuantityValue(value='420',unit='mm'),offer),known(QuantityValue(value='460',unit='mm'),offer)))
    conflicting=offer.model_copy(update={'attributes':dict(offer.attributes,seat_width=conflict)})
    result=compare_offer(uuid4(),input_for(release),conflicting,release.profile,VersionRef(id='matching',version='1.0.0'),NOW)
    assert result.value.classification=='incomplete'
    assert result.value.fields[0].status=='unknown_offer'


def test_catalog_semantics_units_and_stable_incomplete_pagination():
    release=load_demo_release(ROOT)
    from tsr.application.catalog_service import DemoCatalog
    from tsr.contracts import CatalogQuery
    from tsr.domain.catalog import validate_catalog
    offer=release.offers[0]
    wrong=offer.model_copy(update={'attributes':dict(offer.attributes,seat_width=known(QuantityValue(value='420',unit='kg'),offer))})
    pack=release.catalog.model_copy(update={'offers':(wrong,)+release.offers[1:]})
    assert not validate_catalog(pack,(release.profile,),release.sources).valid
    query=CatalogQuery(category_id=release.profile.category_id,profile_ref=release.profile.ref,region_code='ru-alt',limit=2,include_incomplete=True)
    catalog=DemoCatalog(release)
    first=catalog.list_candidates(query).value
    second=catalog.list_candidates(query.model_copy(update={'cursor':first.next_cursor})).value
    assert first.items==release.offers[:2]
    assert second.items==release.offers[2:4]
    assert catalog.get_snapshots((release.offers[2].snapshot_id,),release.release_ref).value==(release.offers[2],)


def test_invalid_matching_input_kind_is_validation_error():
    from tsr.contracts import TextValue
    release=load_demo_release(ROOT)
    inp=input_for(release,prescribed={'seat_width':TextValue(value='420')})
    result=compare_offer(uuid4(),inp,release.offers[0],release.profile,VersionRef(id='matching',version='1.0.0'),NOW)
    assert not result.ok and result.error.code=='VALIDATION_ERROR'


def test_unknown_fact_and_route_addressee_evidence_references_resolve():
    from tsr.contracts import SourceEvidence
    from tsr.domain.catalog import validate_catalog
    from tsr.domain.routes import validate_route_pack
    release=load_demo_release(ROOT)
    missing=SourceEvidence(source_id='missing-source',locator='fixture',observed_at=NOW)
    unknown=Fact.unknown('not_confirmed',evidence=(missing,))
    offer=release.offers[0].model_copy(update={'attributes':dict(release.offers[0].attributes,seat_width=unknown)})
    catalog=release.catalog.model_copy(update={'offers':(offer,)+release.offers[1:]})
    assert not validate_catalog(catalog,(release.profile,),release.sources).valid
    addressee=release.route.addressee.model_copy(update={'evidence':(missing,)})
    route=release.route.model_copy(update={'addressee':addressee})
    assert not validate_route_pack(route,release.sources,release.templates).valid
