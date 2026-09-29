"""Pure case transitions. Callers own ID allocation, clocks, and transactions."""
from datetime import datetime, timedelta
from decimal import Decimal
import hmac
import re
import unicodedata
from uuid import UUID

from pydantic import ValidationError
from tsr.contracts import (
    AccessDecision, ActorContext, AttributeRule, BooleanValue, Candidate,
    CaseDeletionPlan, CaseGuard, CaseMutation, CaseSnapshot, CategoryProfile,
    CodeValue, ComparisonResult, DialogGuard, ErrorCode, INPUT_MONEY_MAX_MINOR,
    InputRevision, InvalidationPlan, Money, MoneyValue, PricingResult,
    QuantityValue, RangeValue, Result, SetValue, TextValue, UnknownValue,
    UserEvidence, VersionRef, content_hash, normalize_decimal,
)


def initialize_input(input_revision_id: UUID, ctx: ActorContext, case_id: UUID,
                     profile: CategoryProfile, role: str, now: datetime) -> InputRevision:
    return InputRevision(input_revision_id=input_revision_id,owner_id=ctx.owner_id,case_id=case_id,
        created_at=now,category_id=profile.category_id,profile_ref=profile.ref,role=role,
        confirmations=(UserEvidence(input_revision_id=input_revision_id,field_key='role',confirmed_at=now),))


def create_case(ctx: ActorContext, case_id: UUID, profile_ref: VersionRef,
                initial_input: InputRevision, now: datetime) -> CaseSnapshot:
    if initial_input.owner_id!=ctx.owner_id or initial_input.case_id!=case_id or initial_input.profile_ref!=profile_ref:
        raise ValueError('initial input ownership or profile mismatch')
    return CaseSnapshot(case_id=case_id,owner_id=ctx.owner_id,mode=ctx.case_mode,
        category_id=initial_input.category_id,profile_ref=profile_ref,
        input_revision_id=initial_input.input_revision_id,last_activity_at=now)


def _failure(code,field=None): return Result.failure(code,field_key=field)


def _live(case): return case.status not in {'deleting','deleted'}


def _parse_money(raw):
    text=raw.strip().replace(',','.')
    if not re.fullmatch(r'\d+(?:\.\d{1,2})?',text): raise ValueError('money requires at most two fractional digits')
    minor=int(Decimal(text)*100)
    if minor>INPUT_MONEY_MAX_MINOR: raise ValueError('input money limit')
    return MoneyValue(value=Money(minor=minor))


def _normalize_value(rule,raw):
    if isinstance(raw,UnknownValue) or raw is None: return raw or UnknownValue()
    if isinstance(raw,str):
        text=unicodedata.normalize('NFC',raw.strip())
        if not text or len(text)>2000: raise ValueError('invalid input length')
        if rule.value_kind=='quantity': value=QuantityValue(value=normalize_decimal(text.replace(',','.')),unit=rule.unit)
        elif rule.value_kind=='money': value=_parse_money(text)
        elif rule.value_kind=='boolean':
            values={'true':True,'false':False,'yes':True,'no':False,'да':True,'нет':False}
            if text.lower() not in values: raise ValueError('boolean required')
            value=BooleanValue(value=values[text.lower()])
        elif rule.value_kind=='code': value=CodeValue(value=text)
        else: value=TextValue(value=text)
    else: value=raw
    if isinstance(value,RangeValue):
        if rule.value_kind!='quantity' or rule.operator!='within_range' or value.unit!=rule.unit: raise ValueError('range incompatible')
    elif isinstance(value,SetValue):
        if rule.operator!='in_set' or any(v.kind!=rule.value_kind for v in value.values): raise ValueError('set incompatible')
        if rule.allowed_codes and any(v.value not in rule.allowed_codes for v in value.values): raise ValueError('code not allowed')
    else:
        if value.kind!=rule.value_kind: raise ValueError('value kind incompatible')
        if rule.operator in {'within_range','in_set'}: raise ValueError('structured requirement required')
        if isinstance(value,QuantityValue) and value.unit!=rule.unit: raise ValueError('unit incompatible')
        if isinstance(value,CodeValue) and rule.allowed_codes and value.value not in rule.allowed_codes: raise ValueError('code not allowed')
        if isinstance(value,MoneyValue) and value.value.minor>INPUT_MONEY_MAX_MINOR: raise ValueError('input money limit')
    return value


def propose_value(case: CaseSnapshot, candidate_id: UUID, field_spec: AttributeRule,
                  raw_value, now: datetime) -> Result[Candidate]:
    if not _live(case): return _failure(ErrorCode.CASE_DELETED)
    try:
        value=_normalize_value(field_spec,raw_value)
        candidate=Candidate(candidate_id=candidate_id,owner_id=case.owner_id,case_id=case.case_id,
            field_key=field_spec.field_key,value=value,value_hash='',case_guard=case.guard,
            dialog_revision=case.dialog_revision+1,created_at=now,expires_at=now+timedelta(seconds=900))
        return Result.success(candidate.model_copy(update={'value_hash':content_hash(candidate)}))
    except (ValueError,TypeError,ValidationError): return _failure(ErrorCode.VALIDATION_ERROR,field_spec.field_key)


def invalidate_dependents(case: CaseSnapshot, change: str) -> InvalidationPlan:
    semantic=change not in {'navigation','heartbeat','delivery','confirmation','dialog'}
    return InvalidationPlan(clear_confirmation=semantic,expire_handles=semantic,
        mark_bundles_historical=semantic,cancel_old_jobs=semantic)


def _mutation(case,updates,input_revision=None):
    new_case=case.model_copy(update={**updates,'case_revision':case.case_revision+1,
        'dialog_revision':case.dialog_revision+1,'active_confirmation_id':None})
    return CaseMutation(new_case=new_case,new_input_revision=input_revision,invalidation=invalidate_dependents(case,'semantic'))


def confirm_value(case: CaseSnapshot, input_revision_id: UUID, candidate: Candidate,
                  candidate_hash: str, previous_input: InputRevision, now: datetime) -> Result[CaseMutation]:
    if not _live(case): return _failure(ErrorCode.CASE_DELETED)
    if candidate.owner_id!=case.owner_id or candidate.case_id!=case.case_id: return _failure(ErrorCode.ACCESS_DENIED)
    if candidate.case_guard!=case.guard or candidate.dialog_revision!=case.dialog_revision or now>=candidate.expires_at:
        return _failure(ErrorCode.STALE_CANDIDATE)
    if not isinstance(candidate_hash,str) or not hmac.compare_digest(candidate_hash,candidate.value_hash) or not hmac.compare_digest(candidate.value_hash,content_hash(candidate)):
        return _failure(ErrorCode.STALE_CANDIDATE)
    if (previous_input.owner_id,previous_input.case_id,previous_input.input_revision_id)!=(case.owner_id,case.case_id,case.input_revision_id):
        return _failure(ErrorCode.STALE_REVISION)
    if previous_input.profile_ref!=case.profile_ref: return _failure(ErrorCode.INCOMPATIBLE_PROFILE)
    key,value=candidate.field_key,candidate.value
    updates={'input_revision_id':input_revision_id,'previous_id':previous_input.input_revision_id,'created_at':now,
        'confirmations':previous_input.confirmations+(UserEvidence(input_revision_id=input_revision_id,field_key=key,confirmed_at=now),)}
    try:
        if key=='region_code':
            if not isinstance(value,(UnknownValue,TextValue)): raise ValueError('region requires text')
            updates[key]=None if isinstance(value,UnknownValue) else value.value
        elif key=='certificate_amount':
            if not isinstance(value,(UnknownValue,MoneyValue)): raise ValueError('amount requires money')
            updates[key]=None if isinstance(value,UnknownValue) else value.value
        elif key=='certificate_applicable_declared':
            answer='unknown' if isinstance(value,UnknownValue) else value.value if isinstance(value,CodeValue) else None
            if answer not in {'yes','no','unknown'}: raise ValueError('declared answer invalid')
            updates[key]=answer
        elif key.startswith('route_answers.'):
            answer='unknown' if isinstance(value,UnknownValue) else value.value if isinstance(value,CodeValue) else None
            if answer not in {'yes','no','unknown'}: raise ValueError('route answer invalid')
            answers=dict(previous_input.route_answers); answers[key.partition('.')[2]]=answer
            updates['route_answers']=answers
        elif key.startswith('document_fields.'):
            if not isinstance(value,(UnknownValue,TextValue)): raise ValueError('document requires text')
            fields=dict(previous_input.document_fields); name=key.partition('.')[2]
            if isinstance(value,UnknownValue): fields.pop(name,None)
            else: fields[name]=value.value
            updates['document_fields']=fields
        else:
            prescribed=dict(previous_input.prescribed)
            unspecified=set(previous_input.unspecified_fields)
            if isinstance(value,UnknownValue): prescribed.pop(key,None); unspecified.add(key)
            else: prescribed[key]=value; unspecified.discard(key)
            updates['prescribed']=prescribed; updates['unspecified_fields']=tuple(sorted(unspecified))
        # model_copy does not validate fields; reconstruct at mutation boundary.
        revision=InputRevision.model_validate({**previous_input.model_dump(mode='python'),**updates})
        return Result.success(_mutation(case,{'input_revision_id':input_revision_id,'last_activity_at':now,'step':'input'},revision))
    except (ValueError,TypeError,ValidationError): return _failure(ErrorCode.VALIDATION_ERROR,key)


def select_snapshot(case: CaseSnapshot, comparison: ComparisonResult, quote: PricingResult,
                    snapshot_id: UUID) -> Result[CaseMutation]:
    if not _live(case): return _failure(ErrorCode.CASE_DELETED)
    if comparison.profile_ref!=case.profile_ref: return _failure(ErrorCode.INCOMPATIBLE_PROFILE)
    if comparison.input_revision_id!=case.input_revision_id or quote.input_revision_id!=case.input_revision_id:
        return _failure(ErrorCode.STALE_REVISION)
    if comparison.snapshot_id!=snapshot_id or quote.snapshot_id!=snapshot_id: return _failure(ErrorCode.VALIDATION_ERROR)
    if case.selected_snapshot_id==snapshot_id:
        return Result.success(CaseMutation(new_case=case,invalidation=invalidate_dependents(case,'navigation')))
    return Result.success(_mutation(case,{'selected_snapshot_id':snapshot_id,'step':'branch'}))


def choose_branch(case: CaseSnapshot, branch: str) -> Result[CaseMutation]:
    if not _live(case): return _failure(ErrorCode.CASE_DELETED)
    if branch not in {'purchase','support'}: return _failure(ErrorCode.VALIDATION_ERROR,'branch')
    if branch==case.branch:
        return Result.success(CaseMutation(new_case=case,invalidation=invalidate_dependents(case,'navigation')))
    return Result.success(_mutation(case,{'branch':branch,'step':'review' if branch=='purchase' else 'route'}))


def authorize_owned(ctx: ActorContext, record_owner: UUID, guard: CaseGuard | None,
                    current: CaseSnapshot) -> Result[AccessDecision]:
    # Foreign/missing records have one safe response without existence disclosure.
    if ctx.owner_id!=record_owner or ctx.owner_id!=current.owner_id: return _failure(ErrorCode.ACCESS_DENIED)
    if not _live(current): return _failure(ErrorCode.CASE_DELETED)
    if guard is not None:
        if guard.case_id!=current.case_id: return _failure(ErrorCode.ACCESS_DENIED)
        if guard.expected_revision!=current.case_revision or guard.expected_deletion_epoch!=current.deletion_epoch:
            return _failure(ErrorCode.STALE_REVISION)
        if isinstance(guard,DialogGuard) and guard.expected_dialog_revision!=current.dialog_revision: return _failure(ErrorCode.STALE_REVISION)
    return Result.success(AccessDecision(allowed=True))


def mark_case_deleted(case: CaseSnapshot, now: datetime) -> CaseDeletionPlan:
    return CaseDeletionPlan(new_epoch=case.deletion_epoch if case.status in {'deleting','deleted'} else case.deletion_epoch+1,
        access_revoked_at=now,cancel_pending=True)
