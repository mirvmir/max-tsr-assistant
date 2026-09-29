"""Fixed, non-executable route predicates with explicit three-state evidence."""
from tsr.contracts import (ConditionResult, Fact, FieldError, FreshnessDecision, Result,
    RouteContext, RouteEvaluation, ValidationReport, VersionRef)

PREDICATES = frozenset({'applicant_status_declared','certificate_applicable_declared',
    'supplier_accepts_certificate','supplier_has_fund_contract'})


def assess_route_freshness(pack,lifecycle,now):
    ref=VersionRef(id=pack.route_id,version=pack.version)
    reasons=[]
    if lifecycle and (lifecycle.ref != ref or lifecycle.status in ('revoked','expired')):
        reasons.append('data.revoked' if lifecycle.status == 'revoked' else 'data.expired')
    if pack.review.status == 'reviewed' and (pack.review.review_due_at is None or now >= pack.review.review_due_at):
        reasons.append('data.expired')
    if pack.review.status != 'reviewed':
        reasons.append('data.draft')
    return FreshnessDecision(decision='blocked' if reasons else 'current',reasons=tuple(reasons),checked_versions=(ref,),checked_at=now)


def validate_route_pack(pack,sources,template_registry=None):
    errors=[]
    source_ids={source.source_id for source in (sources.sources if hasattr(sources,'sources') else sources)}
    ids=[]
    for index,condition in enumerate(pack.conditions):
        ids.append(condition.condition_id)
        if condition.handler_id not in PREDICATES:
            errors.append(FieldError(path=f'conditions[{index}].handler_id',code='UNKNOWN_PREDICATE',safe_message_key='data.invalid_predicate'))
        if not condition.source_ids or set(condition.source_ids)-source_ids:
            errors.append(FieldError(path=f'conditions[{index}].source_ids',code='INVALID_REFERENCE',safe_message_key='data.invalid_reference'))
    if len(set(ids)) != len(ids):
        errors.append(FieldError(path='conditions',code='DUPLICATE_ID',safe_message_key='data.duplicate_id'))
    checklist_ids=[item.item_id for item in pack.checklist]
    if len(set(checklist_ids)) != len(checklist_ids):
        errors.append(FieldError(path='checklist',code='DUPLICATE_ID',safe_message_key='data.duplicate_id'))
    for index,item in enumerate(pack.checklist):
        if set(item.source_ids)-source_ids:
            errors.append(FieldError(path=f'checklist[{index}].source_ids',code='INVALID_REFERENCE',safe_message_key='data.invalid_reference'))
    addressee_facts=(pack.addressee,)+tuple(pack.addressee.alternatives)
    for fact in addressee_facts:
        for evidence in fact.evidence:
            if evidence.kind=='source' and evidence.source_id not in source_ids:
                errors.append(FieldError(path='addressee.evidence',code='INVALID_REFERENCE',safe_message_key='data.invalid_reference'))
    if set(pack.source_ids)-source_ids:
        errors.append(FieldError(path='source_ids',code='INVALID_REFERENCE',safe_message_key='data.invalid_reference'))
    return ValidationReport(valid=not errors,errors=tuple(errors),warnings=('route.synthetic_model',) if pack.data_kind=='synthetic' else ())


def build_route_context(input_revision,snapshot,policy,now):
    answers=dict(input_revision.route_answers)
    applicant=answers.pop('applicant_status_declared','unknown')
    return RouteContext(region_code=input_revision.region_code,category_id=input_revision.category_id,
        role=input_revision.role,applicant_status_declared=applicant,
        certificate_applicable_declared=input_revision.certificate_applicable_declared,
        additional_answers=answers,supplier_accepts_certificate=snapshot.accepts_certificate,
        supplier_has_fund_contract=snapshot.has_fund_contract,policy=policy,now=now)


def build_checklist(pack,role,answers):
    items=[]
    for item in pack.checklist:
        applicability='required' if item.required_when=='always' or item.required_when=='representative' and role=='representative' else 'optional' if item.required_when=='if_available' else 'not_applicable'
        answer=answers.get(item.item_id,'unknown')
        status='provided' if answer=='yes' else 'missing' if answer=='no' else 'unknown'
        items.append(item.model_copy(update={'applicability':applicability,'user_status':status}))
    return tuple(items)


def _predicate(condition,context):
    if condition.handler_id in ('applicant_status_declared','certificate_applicable_declared'):
        value=getattr(context,condition.handler_id)
        return ('met' if value=='yes' else 'not_met' if value=='no' else 'unknown'),()
    if condition.handler_id in ('supplier_accepts_certificate','supplier_has_fund_contract'):
        fact=getattr(context,condition.handler_id)
        if fact.status!='known':
            return 'unknown',fact.evidence
        return ('met' if fact.value.value is True else 'not_met'),fact.evidence
    raise ValueError('unregistered predicate')


def evaluate_route(context,pack_or_none,lifecycle,now):
    pack=pack_or_none
    def empty(status,missing=()):
        return Result.success(RouteEvaluation(route_ref=None,status=status,conditions=(),checklist=(),
            addressee=Fact.unknown('route.not_covered'),next_steps=(),missing_fields=missing,evaluated_at=now))
    if pack is None:
        return empty('needs_clarification',('region_code',)) if context.region_code is None else empty('not_covered')
    if context.category_id!=pack.category_id or context.region_code is not None and context.region_code!=pack.region_code:
        return empty('not_covered')
    ref=VersionRef(id=pack.route_id,version=pack.version)
    if lifecycle and lifecycle.ref != ref:
        return Result.failure('DATA_NOT_READY')
    if lifecycle and lifecycle.status=='revoked':
        return Result.failure('DATA_REVOKED')
    if lifecycle and lifecycle.status=='expired' or pack.review.status=='reviewed' and (pack.review.review_due_at is None or now>=pack.review.review_due_at):
        return Result.failure('DATA_EXPIRED')
    if pack.review.status!='reviewed' and not (context.policy.case_mode=='demo' and context.policy.allow_synthetic_draft and pack.data_kind=='synthetic' and pack.review.status=='draft'):
        return Result.failure('DATA_NOT_READY')
    if pack.review.status=='reviewed' and (pack.review.reviewed_at is None or pack.review.reviewed_at>now):
        return Result.failure('DATA_NOT_READY')
    conditions=[]
    missing=['region_code'] if context.region_code is None else []
    blocked=False
    for condition in pack.conditions:
        try:
            status,evidence=_predicate(condition,context)
        except ValueError:
            return Result.failure('VALIDATION_ERROR',field_key=condition.condition_id)
        conditions.append(ConditionResult(condition_id=condition.condition_id,status=status,evidence_refs=tuple(item.source_id if item.kind=='source' else str(item.input_revision_id) if item.kind=='user' else item.algorithm.id for item in evidence),reason_code='route.'+status))
        if condition.required and status=='not_met':
            blocked=True
        if condition.required and status=='unknown':
            missing.append(condition.handler_id)
    if pack.addressee.status!='known':
        missing.append('addressee')
    status='blocked' if blocked else 'needs_clarification' if missing else 'preliminary_match'
    next_steps=tuple(pack.next_steps)
    if pack.data_kind=='synthetic':
        next_steps=('route.synthetic_model',)+next_steps
    return Result.success(RouteEvaluation(route_ref=ref,status=status,conditions=tuple(conditions),
        checklist=build_checklist(pack,context.role,context.additional_answers),addressee=pack.addressee,
        next_steps=next_steps,missing_fields=tuple(missing),evaluated_at=now))
