"""Version-exact catalog validation and lifecycle assessment."""
from tsr.contracts import Fact, FieldError, FreshnessDecision, ValidationReport, VersionRef


def validate_catalog(pack,profiles,sources):
    profiles=(profiles,) if hasattr(profiles,'profile_id') else profiles
    profile_map={(item.profile_id,item.version):item for item in profiles}
    source_ids={source.source_id for source in (sources.sources if hasattr(sources,'sources') else sources)}
    supplier_ids={item.supplier_id for item in pack.suppliers}
    errors=[]
    def error(path,code='VALIDATION_ERROR'):
        errors.append(FieldError(path=path,code=code,safe_message_key='data.invalid'))
    if len(supplier_ids)!=len(pack.suppliers):
        error('suppliers','DUPLICATE_ID')
    snapshots=set()
    variants={}
    def check_fact(fact,kind,path,rule=None):
        known=(fact,) if fact.status=='known' else fact.alternatives if fact.status=='conflicting' else ()
        alternatives=set()
        for evidence in fact.evidence:
            if evidence.kind=='source' and evidence.source_id not in source_ids:
                error(path+'.evidence','INVALID_REFERENCE')
        for value in known:
            if value.value.kind!=kind:
                error(path+'.value.kind')
            if kind=='quantity' and rule and value.value.kind=='quantity' and value.value.unit!=rule.unit:
                error(path+'.value.unit')
            if kind=='code' and rule and value.value.kind=='code' and rule.allowed_codes and value.value.value not in rule.allowed_codes:
                error(path+'.value.value')
            if kind=='money' and value.value.kind=='money' and value.value.value.minor>10_000_000_000:
                error(path+'.value.value.minor')
            identity=value.value.model_dump_json()
            if identity in alternatives:
                error(path+'.alternatives','DUPLICATE_VALUE')
            alternatives.add(identity)
            for evidence in value.evidence:
                if evidence.kind=='source' and evidence.source_id not in source_ids:
                    error(path+'.evidence','INVALID_REFERENCE')
    for index,offer in enumerate(pack.offers):
        path=f'offers[{index}]'
        if offer.snapshot_id in snapshots:
            error(path+'.snapshot_id','DUPLICATE_ID')
        snapshots.add(offer.snapshot_id)
        identity=offer.variant.model_dump_json()
        if offer.variant.variant_id in variants and variants[offer.variant.variant_id]!=identity:
            error(path+'.variant','SKU_IDENTITY_CONFLICT')
        variants[offer.variant.variant_id]=identity
        profile=profile_map.get((offer.profile_ref.id,offer.profile_ref.version))
        if profile is None:
            error(path+'.profile_ref','INVALID_REFERENCE')
            continue
        if offer.variant.category_id!=profile.category_id:
            error(path+'.variant.category_id')
        if offer.supplier_id not in supplier_ids or set(offer.source_ids)-source_ids:
            error(path+'.source_ids','INVALID_REFERENCE')
        rules={rule.field_key:rule for rule in profile.fields}
        for key,fact in offer.attributes.items():
            if key not in rules:
                error(path+'.attributes.'+key)
                continue
            check_fact(fact,rules[key].value_kind,path+'.attributes.'+key,rules[key])
        check_fact(offer.price,'money',path+'.price')
        check_fact(offer.delivery.charge,'money',path+'.delivery.charge')
        check_fact(offer.delivery.terms,'text',path+'.delivery.terms')
        check_fact(offer.accepts_certificate,'boolean',path+'.accepts_certificate')
        check_fact(offer.has_fund_contract,'boolean',path+'.has_fund_contract')
        check_fact(offer.availability,'code',path+'.availability')
        availability_values=(offer.availability,) if offer.availability.status=='known' else offer.availability.alternatives
        for alternative_index,value in enumerate(availability_values):
            if value.value and value.value.kind=='code' and value.value.value not in ('in_stock','on_order','out_of_stock'):
                suffix='' if offer.availability.status=='known' else f'.alternatives[{alternative_index}]'
                error(path+'.availability'+suffix+'.value.value')
        if offer.price_kind=='unknown' and offer.price.status=='known':
            error(path+'.price_kind')
        charge=offer.delivery.charge
        if offer.delivery.mode=='included' and not (charge.status=='known' and charge.value.kind=='money' and charge.value.value.minor==0):
            error(path+'.delivery')
        if offer.delivery.mode=='unknown' and charge.status!='unknown':
            error(path+'.delivery')
        if offer.delivery.mode=='conflicting' and charge.status!='conflicting':
            error(path+'.delivery')
    for index,supplier in enumerate(pack.suppliers):
        if set(supplier.source_ids)-source_ids:
            error(f'suppliers[{index}].source_ids','INVALID_REFERENCE')
        for fact in supplier.contacts:
            check_fact(fact,'text',f'suppliers[{index}].contacts')
    return ValidationReport(valid=not errors,errors=tuple(errors),warnings=('catalog.synthetic',) if pack.data_kind=='synthetic' else ())


def assess_offer_freshness(snapshot,lifecycle,now):
    ref=VersionRef(id=str(snapshot.snapshot_id),version=snapshot.profile_ref.version)
    reasons=[]
    if lifecycle and lifecycle.status=='revoked': reasons.append('data.revoked')
    if lifecycle and lifecycle.status=='expired': reasons.append('data.expired')
    if snapshot.review.status=='reviewed' and (snapshot.review.review_due_at is None or now>=snapshot.review.review_due_at): reasons.append('data.expired')
    if snapshot.review.status!='reviewed': reasons.append('data.draft')
    decision='historical_only' if reasons and 'data.draft' not in reasons else 'blocked' if reasons else 'current'
    return FreshnessDecision(decision=decision,reasons=tuple(reasons),checked_versions=(ref,),checked_at=now)


def get_field_evidence(snapshot,field_key,sources):
    from tsr.contracts import EvidenceExplanation
    fact=snapshot.attributes.get(field_key,Fact.unknown('missing_attribute'))
    evidence=tuple(fact.evidence)+tuple(item for alt in fact.alternatives for item in alt.evidence)
    ids={item.source_id for item in evidence if item.kind=='source'}
    records=sources.sources if hasattr(sources,'sources') else sources
    return EvidenceExplanation(evidence=evidence,sources=tuple(source for source in records if source.source_id in ids))
