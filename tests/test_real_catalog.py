"""Independent known observations from the live public capture, never fake demo prices."""
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4
import hashlib

from tsr.contracts import CatalogPack,CategoryProfile,InputRevision,Money,QuantityValue,BooleanValue,SourcesRegistry,VersionRef
from tsr.domain.catalog import validate_catalog
from tsr.domain.matching import compare_offer
from tsr.domain.pricing import quote_purchase

ROOT=Path(__file__).parents[1]


def read_real():
    return (
        CatalogPack.model_validate_json((ROOT/'data/catalog/mvp/1.1.0/catalog.json').read_text()),
        CategoryProfile.model_validate_json((ROOT/'data/categories/manual_wheelchair/1.1.0/profile.json').read_text()),
        SourcesRegistry.model_validate_json((ROOT/'data/sources/1.1.0/sources.json').read_text()),
    )


def test_real_selected_sku_units_price_and_conditional_gap():
    catalog,profile,sources=read_real()
    first=next(offer for offer in catalog.offers if offer.supplier_id=='ortonica' and offer.seller_sku=='5048')
    # Constants taken independently from selected ID5048 and visible card, not from normalized output.
    assert first.variant.model=='Base 200'
    assert first.attributes['seat_width'].value.value=='405' #published40.5cm
    assert first.attributes['max_user_mass'].value.value=='130'
    assert first.price.value.value.minor==1450000
    assert first.price_kind=='exact'
    now=datetime(2026,9,30,1,tzinfo=timezone.utc)
    revision=InputRevision(input_revision_id=uuid4(),owner_id=uuid4(),case_id=uuid4(),created_at=now,
        category_id='manual_wheelchair',profile_ref=profile.ref,role='self',
        prescribed={'seat_width':QuantityValue(value='405',unit='mm'),
            'max_user_mass':QuantityValue(value='100',unit='kg'),'foldable':BooleanValue(value=True)},
        certificate_amount=Money(minor=1000000),certificate_applicable_declared='yes')
    result=compare_offer(uuid4(),revision,first,profile,VersionRef(id='matching',version='1.0.0'),now)
    assert result.ok and result.value.classification=='complete'
    quote=quote_purchase(revision,first,VersionRef(id='pricing',version='1.0.0')).value
    assert quote.certificate_use=='conditional'
    assert quote.coverage.minor==1000000 and quote.gap.minor==450000
    assert quote.possible_own_total is None #no invented free delivery
    assert quote.status=='incomplete'
    from_offer=next(offer for offer in catalog.offers if offer.seller_sku=='00-00046407')
    uncertain=quote_purchase(revision,from_offer,VersionRef(id='pricing',version='1.0.0')).value
    assert uncertain.price is None and uncertain.gap is None


def test_real_catalog_identity_evidence_and_conflicts_preserved():
    catalog,profile,sources=read_real()
    assert len(catalog.offers)==12
    assert {offer.supplier_id for offer in catalog.offers}=={'ortonica','medicamarket'}
    assert len({(offer.supplier_id,offer.seller_sku) for offer in catalog.offers})==12
    assert all(offer.data_kind=='public_snapshot' for offer in catalog.offers)
    assert catalog.review.status=='reviewed'
    assert all(offer.review.status=='reviewed' and offer.review.reviewer_id=='codex-root-public-fact-audit' for offer in catalog.offers)
    assert profile.data_kind=='synthetic' and profile.review.status=='draft' #modelrules remain unapproved
    assert validate_catalog(catalog,(profile,),sources).valid
    real_sources={source.source_id:source for source in sources.sources if source.data_kind=='public_snapshot'}
    assert len(real_sources)==12
    for source in real_sources.values():
        assert source.review.status=='reviewed' and source.review.reviewer_id=='codex-root-public-fact-audit'
        assert source.review.reviewed_at.isoformat()=='2026-09-30T01:10:11.200322+00:00'
        assert source.review.review_due_at.isoformat()=='2026-10-07T01:10:11.200322+00:00'
        assert source.url.startswith(('https://ortonica.ru/','https://medicamarket.ru/'))
        assert hashlib.sha256((ROOT/source.locator).read_bytes()).hexdigest()==source.sha256
    for offer in catalog.offers:
        assert set(offer.source_ids)<=real_sources.keys()
        assert offer.delivery.charge.status=='unknown'
        assert offer.accepts_certificate.status=='unknown'
        assert offer.has_fund_contract.status=='unknown'
    conflict=next(offer for offer in catalog.offers if offer.seller_sku=='00-00048055')
    assert conflict.price.status=='conflicting' and conflict.price_kind=='unknown'
    assert {item.value.value.minor for item in conflict.price.alternatives}=={3314100,3195700}
    unavailable=next(offer for offer in catalog.offers if offer.seller_sku=='00-00048067')
    assert unavailable.availability.status=='conflicting'
    assert {item.value.value for item in unavailable.availability.alternatives}=={'out_of_stock','on_order'}
