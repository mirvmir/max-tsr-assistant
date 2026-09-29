"""Immutable cross-module contracts; no infrastructure imports or side effects."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Annotated, Any, Generic, Literal, TypeVar, Union
from uuid import UUID
import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

T = TypeVar('T')
MONEY_MAX_MINOR = 9_000_000_000_000_000
INPUT_MONEY_MAX_MINOR = 10_000_000_000


class FrozenDict(dict):
    def _immutable(self, *args, **kwargs):
        raise TypeError('immutable DTO mapping')
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable
    def __copy__(self): return self
    def __deepcopy__(self, memo): return self


def _freeze(value):
    if isinstance(value, dict): return FrozenDict({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list): return tuple(_freeze(v) for v in value)
    if isinstance(value, tuple): return tuple(_freeze(v) for v in value)
    return value


def _normalize(value):
    if isinstance(value, str): return unicodedata.normalize('NFC', value)
    if isinstance(value, dict):
        normalized = {_normalize(k): _normalize(v) for k, v in value.items()}
        if len(normalized) != len(value): raise ValueError('duplicate normalized keys')
        return normalized
    if isinstance(value, (tuple, list)): return [_normalize(v) for v in value]
    return value


class DTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', populate_by_name=True, validate_default=True)

    @model_validator(mode='before')
    @classmethod
    def normalize_input(cls, value):
        return _normalize(value)

    @field_validator('*', mode='after')
    @classmethod
    def validate_common(cls, value, info):
        if info.field_name == 'schema_version':
            if not re.fullmatch(r'\d+\.\d+\.\d+', value) or value.split('.')[0] != '1':
                raise ValueError('UNSUPPORTED_SCHEMA')
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError('UTC timestamp required')
            return value.astimezone(timezone.utc)
        return value

    @model_validator(mode='after')
    def freeze_maps(self):
        for key in type(self).model_fields:
            object.__setattr__(self, key, _freeze(getattr(self, key)))
        return self


class ErrorCode(str, Enum):
    VALIDATION_ERROR='VALIDATION_ERROR'
    ACCESS_DENIED='ACCESS_DENIED'
    NOT_FOUND='NOT_FOUND'
    STALE_REVISION='STALE_REVISION'
    STALE_CANDIDATE='STALE_CANDIDATE'
    CASE_DELETED='CASE_DELETED'
    DATA_NOT_READY='DATA_NOT_READY'
    DATA_REVOKED='DATA_REVOKED'
    DATA_EXPIRED='DATA_EXPIRED'
    UNSUPPORTED_SCHEMA='UNSUPPORTED_SCHEMA'
    INCOMPATIBLE_PROFILE='INCOMPATIBLE_PROFILE'
    INVALID_TRANSITION='INVALID_TRANSITION'
    LEASE_LOST='LEASE_LOST'
    RATE_LIMITED='RATE_LIMITED'
    TEMPORARY_FAILURE='TEMPORARY_FAILURE'
    PERMANENT_FAILURE='PERMANENT_FAILURE'
    DELIVERY_UNKNOWN='DELIVERY_UNKNOWN'
    ARTIFACT_EXPIRED='ARTIFACT_EXPIRED'


class DomainError(DTO):
    code: ErrorCode
    field_key: str | None = None
    retryability: Literal['none','safe','explicit_user'] = 'none'
    safe_message_key: str
    correlation_id: UUID | None = None


class Result(DTO, Generic[T]):
    ok: bool
    value: T | None = None
    error: DomainError | None = None

    @model_validator(mode='after')
    def exclusive(self):
        if self.ok and (self.error is not None or 'value' not in self.model_fields_set):
            raise ValueError('success requires value and no error')
        if not self.ok and (self.error is None or self.value is not None):
            raise ValueError('failure requires error and no value')
        return self

    @classmethod
    def success(cls, value): return cls(ok=True, value=value)

    @classmethod
    def failure(cls, code, field_key=None, safe_message_key=None, retryability='none', correlation_id=None):
        return cls(ok=False, error=DomainError(code=code,field_key=field_key,retryability=retryability,
            safe_message_key=safe_message_key or f'error.{str(getattr(code,"value",code)).lower()}',correlation_id=correlation_id))


class VersionRef(DTO):
    id: str = Field(pattern=r'^[A-Za-z0-9_.:/-]+$')
    version: str = Field(pattern=r'^\d+\.\d+\.\d+$')


class Money(DTO):
    currency: Literal['RUB'] = 'RUB'
    minor: StrictInt = Field(ge=0, le=MONEY_MAX_MINOR)


class ActorContext(DTO):
    owner_id: UUID
    delivery_target_id: UUID
    bot_scope: str
    case_mode: Literal['demo','pilot']
    correlation_id: UUID


class ReadContext(ActorContext):
    now: datetime


class CaseGuard(DTO):
    case_id: UUID
    expected_revision: int = Field(ge=0)
    expected_deletion_epoch: int = Field(ge=0)


class DialogGuard(CaseGuard):
    expected_dialog_revision: int = Field(ge=0)


class EvaluationPolicy(DTO):
    case_mode: Literal['demo','pilot']
    allow_synthetic_draft: bool = False


def normalize_decimal(value: str) -> str:
    if not isinstance(value,str) or not re.fullmatch(r'-?\d+(?:\.\d+)?', value):
        raise ValueError('finite decimal string required')
    number = Decimal(value)
    normalized = format(number, 'f')
    if '.' in normalized: normalized = normalized.rstrip('0').rstrip('.')
    return '0' if number == 0 else normalized


class QuantityValue(DTO):
    kind: Literal['quantity'] = 'quantity'
    value: str
    unit: Literal['mm','kg']
    _decimal = field_validator('value')(normalize_decimal)


class BooleanValue(DTO):
    kind: Literal['boolean'] = 'boolean'
    value: bool = Field(strict=True)


class CodeValue(DTO):
    kind: Literal['code'] = 'code'
    value: str = Field(pattern=r'^[A-Za-z0-9_.:-]+$')


class TextValue(DTO):
    kind: Literal['text'] = 'text'
    value: str


class MoneyValue(DTO):
    kind: Literal['money'] = 'money'
    value: Money


TypedValue = Annotated[Union[QuantityValue,BooleanValue,CodeValue,TextValue,MoneyValue], Field(discriminator='kind')]


class RangeValue(DTO):
    kind: Literal['range'] = 'range'
    minimum: str
    maximum: str
    unit: Literal['mm','kg']
    _decimal = field_validator('minimum','maximum')(normalize_decimal)
    @model_validator(mode='after')
    def bounds(self):
        if Decimal(self.minimum)>Decimal(self.maximum): raise ValueError('invalid range')
        return self


class SetValue(DTO):
    kind: Literal['set'] = 'set'
    values: tuple[TypedValue, ...] = Field(min_length=1)
    @model_validator(mode='after')
    def members(self):
        keys = [v.model_dump_json() for v in self.values]
        if len(keys)!=len(set(keys)): raise ValueError('duplicate set member')
        if len({(v.kind,getattr(v,'unit',None)) for v in self.values}) != 1: raise ValueError('incompatible set members')
        object.__setattr__(self,'values',tuple(sorted(self.values,key=lambda v:v.model_dump_json())))
        return self


class UnknownValue(DTO):
    kind: Literal['unknown'] = 'unknown'
    reason_code: str = 'unspecified_input'


RequirementValue = Annotated[Union[QuantityValue,BooleanValue,CodeValue,TextValue,MoneyValue,RangeValue,SetValue], Field(discriminator='kind')]
CandidateValue = Annotated[Union[QuantityValue,BooleanValue,CodeValue,TextValue,MoneyValue,RangeValue,SetValue,UnknownValue], Field(discriminator='kind')]


class SourceEvidence(DTO):
    kind: Literal['source'] = 'source'
    source_id: str
    locator: str
    observed_at: datetime


class UserEvidence(DTO):
    kind: Literal['user'] = 'user'
    input_revision_id: UUID
    field_key: str
    confirmed_at: datetime


class CalculatedEvidence(DTO):
    kind: Literal['calculated'] = 'calculated'
    algorithm: VersionRef
    input_refs: tuple[str, ...]


Evidence = Annotated[Union[SourceEvidence,UserEvidence,CalculatedEvidence],Field(discriminator='kind')]


class Fact(DTO, Generic[T]):
    status: Literal['known','unknown','conflicting']
    value: T | None = None
    evidence: tuple[Evidence,...] = ()
    reason_code: str | None = None
    alternatives: tuple[Fact[T],...] = ()

    @model_validator(mode='after')
    def state(self):
        if self.status == 'known':
            if self.value is None or not self.evidence or self.alternatives or self.reason_code is not None:
                raise ValueError('known fact requires value and evidence only')
        elif self.status == 'unknown':
            if self.value is not None or self.alternatives or not self.reason_code:
                raise ValueError('unknown fact requires reason and null value')
        else:
            if self.value is not None or not self.reason_code or len(self.alternatives)<2 or self.evidence:
                raise ValueError('conflict requires distinct known alternatives')
            if any(a.status!='known' for a in self.alternatives): raise ValueError('known alternatives required')
            keys = [a.value.model_dump_json() if isinstance(a.value,BaseModel) else str(a.value) for a in self.alternatives]
            if len(set(keys))!=len(keys): raise ValueError('distinct alternatives required')
        return self

    @classmethod
    def known(cls,value,evidence): return cls(status='known',value=value,evidence=evidence)
    @classmethod
    def unknown(cls,reason_code='not_known',evidence=()): return cls(status='unknown',reason_code=reason_code,evidence=evidence)


class Review(DTO):
    status: Literal['draft','reviewed'] = 'draft'
    reviewer_id: str | None = None
    reviewed_at: datetime | None = None
    review_due_at: datetime | None = None
    @model_validator(mode='after')
    def reviewed(self):
        if self.status=='reviewed' and (not self.reviewer_id or self.reviewed_at is None or self.review_due_at is None):
            raise ValueError('review evidence required')
        if self.reviewed_at and self.review_due_at and self.review_due_at <= self.reviewed_at: raise ValueError('review interval invalid')
        return self


class LifecycleRecord(DTO):
    ref: VersionRef
    status: Literal['active','revoked','expired'] = 'active'
    changed_at: datetime | None = None
    reason_code: str | None = None


class SourceMetadata(DTO):
    source_id: str
    title: str
    url: str | None = None
    locator: str = ''
    observed_at: datetime | None = None
    use_basis: str = 'synthetic'


class SourceRecord(SourceMetadata):
    data_kind: Literal['synthetic','public_snapshot'] = 'synthetic'
    review: Review = Field(default_factory=Review)
    captured_at: datetime | None = None
    sha256: str | None = None


class AttributeRule(DTO):
    field_key: str
    label: str
    value_kind: Literal['quantity','boolean','code','text','money']
    unit: Literal['mm','kg'] | None = None
    operator: Literal['eq','gte','lte','within_range','in_set'] = 'eq'
    required_for_complete_comparison: bool = False
    allowed_codes: tuple[str,...] = ()
    tolerance: str | None = None
    help_text: str = ''
    source_ids: tuple[str,...] = ()
    @model_validator(mode='after')
    def compatible(self):
        allowed={'quantity':{'eq','gte','lte','within_range'},'code':{'eq','in_set'},'boolean':{'eq'},'text':{'eq'},'money':{'eq','gte','lte'}}
        if self.operator not in allowed[self.value_kind]: raise ValueError('operator incompatible')
        if (self.value_kind=='quantity') != (self.unit is not None): raise ValueError('unit incompatible')
        if self.tolerance is not None:
            if self.value_kind!='quantity' or Decimal(normalize_decimal(self.tolerance))<0: raise ValueError('tolerance incompatible')
        return self


class CategoryProfile(DTO):
    schema_version: str = '1.0.0'
    profile_id: str
    version: str = Field(pattern=r'^\d+\.\d+\.\d+$')
    category_id: str
    data_kind: Literal['synthetic','public_snapshot']
    review: Review
    fields: tuple[AttributeRule,...]
    @property
    def ref(self): return VersionRef(id=self.profile_id,version=self.version)
    @model_validator(mode='after')
    def unique_fields(self):
        if len({f.field_key for f in self.fields})!=len(self.fields): raise ValueError('duplicate field key')
        return self


class InputRevision(DTO):
    input_revision_id: UUID
    owner_id: UUID
    case_id: UUID
    created_at: datetime
    previous_id: UUID | None = None
    category_id: str
    profile_ref: VersionRef
    region_code: str | None = None
    role: Literal['self','representative']
    prescribed: Mapping[str,RequirementValue] = Field(default_factory=dict)
    unspecified_fields: tuple[str,...] = ()
    certificate_amount: Money | None = None
    certificate_applicable_declared: Literal['yes','no','unknown'] = 'unknown'
    route_answers: Mapping[str,Literal['yes','no','unknown']] = Field(default_factory=dict)
    document_fields: Mapping[str,str] = Field(default_factory=dict)
    confirmations: tuple[UserEvidence,...] = ()
    @model_validator(mode='after')
    def input_rules(self):
        if set(self.prescribed)&set(self.unspecified_fields): raise ValueError('prescribed and unspecified overlap')
        if len(set(self.unspecified_fields))!=len(self.unspecified_fields): raise ValueError('duplicate unspecified fields')
        if self.certificate_amount and self.certificate_amount.minor>INPUT_MONEY_MAX_MINOR: raise ValueError('input money limit')
        return self


class Variant(DTO):
    variant_id: str
    category_id: str
    manufacturer: str
    model: str
    modification: str
    configuration: str


class Supplier(DTO):
    supplier_id: str
    display_name: str
    contacts: tuple[Fact[TextValue],...] = ()
    source_ids: tuple[str,...] = ()


class Delivery(DTO):
    mode: Literal['included','separate','unknown','conflicting']
    charge: Fact[MoneyValue]
    terms: Fact[TextValue]
    @model_validator(mode='after')
    def delivery_rules(self):
        if self.mode=='included' and (self.charge.status!='known' or self.charge.value.value.minor!=0): raise ValueError('included delivery requires evidenced zero')
        if self.mode=='unknown' and self.charge.status!='unknown': raise ValueError('unknown delivery requires unknown charge')
        if self.charge.status=='known' and self.charge.value.value.minor>INPUT_MONEY_MAX_MINOR: raise ValueError('input money limit')
        return self


class OfferSnapshot(DTO):
    snapshot_id: UUID
    offer_id: str
    supplier_id: str
    seller_sku: str
    variant: Variant
    profile_ref: VersionRef
    data_kind: Literal['synthetic','public_snapshot']
    observed_at: datetime
    review: Review
    attributes: Mapping[str,Fact[TypedValue]]
    price: Fact[MoneyValue]
    price_kind: Literal['exact','from','unknown']
    delivery: Delivery
    accepts_certificate: Fact[BooleanValue]
    has_fund_contract: Fact[BooleanValue]
    availability: Fact[CodeValue]
    source_ids: tuple[str,...]
    @model_validator(mode='after')
    def offer_rules(self):
        if self.price_kind=='unknown' and self.price.status=='known': raise ValueError('unknown price cannot be exact')
        for fact in (self.price,*self.price.alternatives):
            if fact.status=='known' and fact.value.value.minor>INPUT_MONEY_MAX_MINOR: raise ValueError('input money limit')
        for fact in (self.availability,*self.availability.alternatives):
            if fact.status=='known' and fact.value.value not in {'in_stock','on_order','out_of_stock'}: raise ValueError('availability code invalid')
        return self


class CatalogQuery(DTO):
    category_id: str
    profile_ref: VersionRef
    region_code: str | None = None
    cursor: str | None = None
    limit: int = Field(default=20,ge=1,le=20)
    include_incomplete: Literal[True] = True


class OfferPage(DTO):
    items: tuple[OfferSnapshot,...]
    next_cursor: str | None = None
    active_catalog_ref: VersionRef
    data_quality_flags: tuple[str,...] = ()


class CaseSnapshot(DTO):
    case_id: UUID
    owner_id: UUID
    mode: Literal['demo','pilot']
    category_id: str
    profile_ref: VersionRef
    case_revision: int = Field(default=0,ge=0)
    dialog_revision: int = Field(default=0,ge=0)
    deletion_epoch: int = Field(default=0,ge=0)
    status: Literal['active','completed','deleting','deleted'] = 'active'
    step: Literal['start','input','comparison','selection','branch','route','review','preparing','materials'] = 'input'
    input_revision_id: UUID
    selected_snapshot_id: UUID | None = None
    branch: Literal['purchase','support'] | None = None
    active_confirmation_id: UUID | None = None
    last_activity_at: datetime
    @property
    def guard(self): return CaseGuard(case_id=self.case_id,expected_revision=self.case_revision,expected_deletion_epoch=self.deletion_epoch)


class Candidate(DTO):
    candidate_id: UUID
    owner_id: UUID
    case_id: UUID
    field_key: str
    value: CandidateValue
    value_hash: str
    case_guard: CaseGuard
    dialog_revision: int = Field(ge=0)
    created_at: datetime
    expires_at: datetime


class SupplierQuestion(DTO):
    question_key: str
    field_key: str | None = None
    text_key: str
    source_refs: tuple[str,...] = ()
    category: Literal['parameter','price','delivery','certificate','fund_contract','availability']


class FieldMatch(DTO):
    field_key: str
    status: Literal['match','mismatch','unknown_offer','unspecified_input']
    required_for_complete: bool
    required_value: RequirementValue | None = None
    offer_fact: Fact[TypedValue]
    reason_code: str
    evidence_refs: tuple[str,...] = ()
    supplier_question: SupplierQuestion | None = None


class ComparisonResult(DTO):
    comparison_id: UUID
    input_revision_id: UUID
    snapshot_id: UUID
    profile_ref: VersionRef
    matching_algorithm_ref: VersionRef
    computed_at: datetime
    fields: tuple[FieldMatch,...]
    classification: Literal['complete','incomplete','mismatch'] = Field(alias='class')
    questions: tuple[SupplierQuestion,...] = ()


class PricingResult(DTO):
    pricing_algorithm_ref: VersionRef
    input_revision_id: UUID
    snapshot_id: UUID
    price: Money | None = None
    certificate_limit: Money | None = None
    certificate_use: Literal['allowed_by_declared_data','conditional','not_accepted','not_applicable','unknown']
    coverage: Money | None = None
    gap: Money | None = None
    delivery: Delivery
    possible_own_total: Money | None = None
    status: Literal['calculated','conditional','incomplete']
    reasons: tuple[str,...] = ()
    assumptions: tuple[str,...] = ()
    provenance: CalculatedEvidence


class RouteContext(DTO):
    region_code: str | None
    category_id: str
    role: Literal['self','representative']
    applicant_status_declared: Literal['yes','no','unknown']
    certificate_applicable_declared: Literal['yes','no','unknown']
    additional_answers: Mapping[str,Literal['yes','no','unknown']] = Field(default_factory=dict)
    supplier_accepts_certificate: Fact[BooleanValue]
    supplier_has_fund_contract: Fact[BooleanValue]
    policy: EvaluationPolicy
    now: datetime


class ConditionResult(DTO):
    condition_id: str
    status: Literal['met','not_met','unknown']
    evidence_refs: tuple[str,...] = ()
    reason_code: str


class ChecklistItem(DTO):
    item_id: str
    label: str
    required_when: Literal['always','if_available','representative']
    source_ids: tuple[str,...] = ()
    applicability: Literal['required','optional','not_applicable'] = 'required'
    user_status: Literal['provided','missing','unknown'] = 'unknown'


class RouteCondition(DTO):
    condition_id: str
    handler_id: str
    required: bool = True
    source_ids: tuple[str,...] = ()
    label: str = ''


class RoutePack(DTO):
    schema_version: str = '1.0.0'
    route_id: str
    version: str
    region_code: str
    category_id: str
    data_kind: Literal['synthetic','public_snapshot']
    review: Review
    conditions: tuple[RouteCondition,...]
    checklist: tuple[ChecklistItem,...]
    addressee: Fact[TextValue]
    next_steps: tuple[str,...] = ()
    source_ids: tuple[str,...] = ()
    @property
    def ref(self): return VersionRef(id=self.route_id,version=self.version)


class RouteEvaluation(DTO):
    route_ref: VersionRef | None = None
    status: Literal['preliminary_match','blocked','needs_clarification','not_covered']
    conditions: tuple[ConditionResult,...] = ()
    checklist: tuple[ChecklistItem,...] = ()
    addressee: Fact[TextValue]
    next_steps: tuple[str,...] = ()
    missing_fields: tuple[str,...] = ()
    evaluated_at: datetime


class FreshnessDecision(DTO):
    decision: Literal['current','historical_only','blocked']
    reasons: tuple[str,...] = ()
    checked_versions: tuple[VersionRef,...] = ()
    checked_at: datetime


class InvalidationPlan(DTO):
    clear_confirmation: bool = True
    expire_handles: bool = True
    mark_bundles_historical: bool = True
    cancel_old_jobs: bool = True


class CaseMutation(DTO):
    new_case: CaseSnapshot
    new_input_revision: InputRevision | None = None
    invalidation: InvalidationPlan


class AccessDecision(DTO):
    allowed: bool
    scope: Literal['owned'] = 'owned'


class CaseDeletionPlan(DTO):
    new_epoch: int
    access_revoked_at: datetime
    cancel_pending: Literal[True] = True


class StartCasePayload(DTO):
    category_ref: VersionRef
    requested_role: Literal['self','representative']
class ProposeFieldPayload(DTO):
    field_key: str
    raw_text: str | None = None
    unknown: bool = False
    @model_validator(mode='after')
    def exclusive(self):
        if (self.raw_text is None) == (not self.unknown): raise ValueError('raw_text or explicit unknown required')
        return self
class ConfirmCandidatePayload(DTO):
    candidate_id: UUID
    candidate_hash: str
class CompareOffersPayload(DTO):
    snapshot_ids: tuple[UUID,...] = Field(min_length=1,max_length=3)
class SelectOfferPayload(DTO):
    snapshot_id: UUID
    comparison_id: UUID
class ChooseBranchPayload(DTO):
    branch: Literal['purchase','support']
class AnswerRoutePayload(DTO):
    field_key: str
    answer: Literal['yes','no','unknown']
class ConfirmResultPayload(DTO):
    preview_id: UUID
    manifest_hash: str
class RequestMaterialPayload(DTO):
    artifact_id: UUID
    disposition: Literal['current','historical']
class RetryDeliveryPayload(DTO):
    outbox_id: UUID
    acknowledged_possible_duplicate: bool
class NavigatePayload(DTO):
    destination: Literal['back','resume','materials','help']
    page: int = Field(default=0,ge=0)
    resource_id: UUID | None = None
    screen: Literal['candidate','review','materials'] | None = None
    @model_validator(mode='after')
    def page_scope(self):
        if self.screen in {'candidate','review'} and self.resource_id is None:
            raise ValueError('frozen pagination requires resource')
        if self.screen not in {'candidate','review'} and self.resource_id is not None:
            raise ValueError('resource requires frozen screen')
        if self.screen is None and self.page != 0:
            raise ValueError('page requires screen')
        return self
class DeleteCasePayload(DTO):
    confirmation_handle: str

CommandPayload = Union[StartCasePayload,ProposeFieldPayload,ConfirmCandidatePayload,CompareOffersPayload,SelectOfferPayload,ChooseBranchPayload,AnswerRoutePayload,ConfirmResultPayload,RequestMaterialPayload,RetryDeliveryPayload,NavigatePayload,DeleteCasePayload]
COMMAND_PAYLOADS = {
    'start_case':StartCasePayload,'propose_field':ProposeFieldPayload,'confirm_candidate':ConfirmCandidatePayload,
    'compare_offers':CompareOffersPayload,'select_offer':SelectOfferPayload,'choose_branch':ChooseBranchPayload,
    'answer_route':AnswerRoutePayload,'confirm_result':ConfirmResultPayload,'request_material':RequestMaterialPayload,
    'retry_delivery':RetryDeliveryPayload,'navigate':NavigatePayload,'delete_case':DeleteCasePayload,
}
CommandType = Literal['start_case','propose_field','confirm_candidate','compare_offers','select_offer','choose_branch','answer_route','confirm_result','request_material','retry_delivery','navigate','delete_case']


class CommandEnvelope(DTO):
    command_id: UUID
    inbox_id: UUID | None = None
    actor: ActorContext
    case_guard: CaseGuard | None = None
    dialog_guard: DialogGuard | None = None
    type: CommandType
    payload: CommandPayload
    @model_validator(mode='after')
    def typed_payload(self):
        if not isinstance(self.payload,COMMAND_PAYLOADS[self.type]): raise ValueError('command payload mismatch')
        if self.dialog_guard is not None and self.case_guard is not None:
            if (self.dialog_guard.case_id,self.dialog_guard.expected_revision,self.dialog_guard.expected_deletion_epoch)!=(self.case_guard.case_id,self.case_guard.expected_revision,self.case_guard.expected_deletion_epoch):
                raise ValueError('guard mismatch')
        return self


SafeValue = str | int | bool | Money | None
SafeParameters = Mapping[str,SafeValue]


class ViewSection(DTO):
    kind: Literal['text','offer','comparison','status','material'] = 'text'
    text_key: str
    parameters: SafeParameters = Field(default_factory=dict)


class ActionIntent(DTO):
    action_key: str
    label_key: str
    command_type: CommandType
    typed_payload: CommandPayload
    required_guard: CaseGuard | DialogGuard | None = None
    @model_validator(mode='after')
    def payload_matches(self):
        if not isinstance(self.typed_payload,COMMAND_PAYLOADS[self.command_type]): raise ValueError('action payload mismatch')
        return self


ActionDescriptor = ActionIntent


class ActionSpec(DTO):
    label_key: str
    action_handle: str
    expires_at: datetime


class ViewDraft(DTO):
    kind: str
    title_key: str
    sections: tuple[ViewSection,...] = ()
    actions: tuple[ActionIntent,...] = ()
    case_guard: CaseGuard | None = None
    dialog_revision: int | None = None
    flags: tuple[str,...] = ()


class ViewModel(DTO):
    view_id: UUID
    kind: str
    title_key: str
    sections: tuple[ViewSection,...] = ()
    actions: tuple[ActionSpec,...] = ()
    case_guard: CaseGuard | None = None
    dialog_revision: int | None = None
    flags: tuple[str,...] = ()


class CommandResult(DTO):
    case: CaseSnapshot | None
    view: ViewModel
    created_bundle_id: UUID | None = None
    delivery_state: Literal['queued','delivery_unknown'] | None = None


class ComparisonSet(DTO):
    items: tuple[ComparisonResult,...]
    quotes: tuple[PricingResult,...]
    case_guard: CaseGuard


class TextEvent(DTO):
    text: str
    reply_to_message_id: str | None = None
class CallbackEvent(DTO):
    platform_callback_id: str
    action_handle: str
class StartEvent(DTO):
    payload: str | None = None
class IgnoredEvent(DTO):
    pass


class NormalizedEvent(DTO):
    inbox_id: UUID
    bot_scope: str
    dedupe_key: str
    owner_id: UUID
    delivery_target_id: UUID
    kind: Literal['start','text','callback','ignored']
    occurred_at: datetime
    received_at: datetime
    payload: TextEvent | CallbackEvent | StartEvent | IgnoredEvent
    @model_validator(mode='after')
    def event_matches(self):
        if not isinstance(self.payload,{'start':StartEvent,'text':TextEvent,'callback':CallbackEvent,'ignored':IgnoredEvent}[self.kind]):
            raise ValueError('event payload mismatch')
        return self


class CallbackAnswer(DTO):
    owner_id: UUID
    platform_callback_id: str
    text_key: str
    parameters: SafeParameters = Field(default_factory=dict)
    inbox_id: UUID


class ArtifactSpec(DTO):
    document_kind: Literal['purchase_card','application','product_card','checklist']
    format: Literal['pdf','docx']
    template_ref: VersionRef


class ManifestContent(DTO):
    schema_version: str = '1.0.0'
    owner_id: UUID
    case_id: UUID
    case_revision: int
    deletion_epoch: int
    case_mode: Literal['demo','pilot']
    input_revision: InputRevision
    offer_snapshot: OfferSnapshot
    sources: tuple[SourceMetadata,...]
    comparison: ComparisonResult
    pricing: PricingResult
    route: RouteEvaluation | None = None
    branch: Literal['purchase','support']
    category_profile_ref: VersionRef
    matching_algorithm_ref: VersionRef
    pricing_algorithm_ref: VersionRef
    route_ref: VersionRef | None = None
    template_refs: tuple[VersionRef,...]
    generator_ref: VersionRef
    release_commit: str
    missing_fields: tuple[str,...] = ()
    flags: tuple[str,...] = ()
    @model_validator(mode='after')
    def coherent(self):
        i=self.input_revision
        if (i.owner_id,i.case_id)!=(self.owner_id,self.case_id): raise ValueError('manifest input ownership mismatch')
        if i.profile_ref!=self.category_profile_ref or self.offer_snapshot.profile_ref!=self.category_profile_ref or self.comparison.profile_ref!=self.category_profile_ref: raise ValueError('manifest profile mismatch')
        if self.comparison.input_revision_id!=i.input_revision_id or self.pricing.input_revision_id!=i.input_revision_id: raise ValueError('manifest input revision mismatch')
        if self.comparison.snapshot_id!=self.offer_snapshot.snapshot_id or self.pricing.snapshot_id!=self.offer_snapshot.snapshot_id: raise ValueError('manifest snapshot mismatch')
        if self.matching_algorithm_ref!=self.comparison.matching_algorithm_ref or self.pricing_algorithm_ref!=self.pricing.pricing_algorithm_ref: raise ValueError('manifest algorithm mismatch')
        if self.route and self.route.route_ref!=self.route_ref: raise ValueError('manifest route mismatch')
        return self


class ResultPreview(DTO):
    preview_id: UUID
    owner_id: UUID
    case_guard: CaseGuard
    dialog_revision: int
    proposed_content: ManifestContent
    manifest_hash: str
    freshness: FreshnessDecision
    expires_at: datetime


class Confirmation(DTO):
    confirmation_id: UUID
    owner_id: UUID
    case_id: UUID
    case_revision: int
    deletion_epoch: int
    preview_id: UUID
    manifest_hash: str
    confirmed_at: datetime
    confirmed_missing_fields: tuple[str,...] = ()


class DocumentManifest(DTO):
    manifest_id: UUID
    manifest_hash: str
    created_at: datetime
    confirmation_id: UUID
    content: ManifestContent
    @property
    def owner_id(self): return self.content.owner_id
    @property
    def case_id(self): return self.content.case_id


class DocumentBundle(DTO):
    bundle_id: UUID
    owner_id: UUID
    case_id: UUID
    manifest_id: UUID
    status: Literal['preparing','ready','failed','historical','expired','deleted'] = 'preparing'
    required_artifacts: tuple[ArtifactSpec,...]
    published_artifact_ids: tuple[UUID,...] = ()
    created_at: datetime
    @model_validator(mode='after')
    def unique_specs(self):
        pairs={(s.document_kind,s.format) for s in self.required_artifacts}
        if len(pairs)!=len(self.required_artifacts): raise ValueError('duplicate artifact spec')
        return self


class DocumentField(DTO):
    label: str
    value: str
class DocumentTable(DTO):
    headers: tuple[str,...]
    rows: tuple[tuple[str,...],...]
class DocumentSection(DTO):
    title: str = ''
    text: str = ''
    fields: tuple[DocumentField,...] = ()
    tables: tuple[DocumentTable,...] = ()
class DocumentModel(DTO):
    schema_version: str = '1.0.0'
    document_kind: Literal['purchase_card','application','product_card','checklist']
    case_mode: Literal['demo','pilot']
    sections: tuple[DocumentSection,...]
    missing_fields: tuple[str,...] = ()
    sources: tuple[SourceMetadata,...] = ()
    warnings: tuple[str,...] = ()
    manifest_hash: str
class DocumentRequest(DTO):
    branch: Literal['purchase','support']
    manifest_id: UUID
    required_artifacts: tuple[ArtifactSpec,...]
class RenderRequest(DTO):
    job_id: UUID
    fence_token: int
    manifest_hash: str
    document_kind: Literal['purchase_card','application','product_card','checklist']
    format: Literal['pdf','docx']
    template_ref: VersionRef
    model: DocumentModel
    max_output_bytes: int = Field(gt=0)
class RenderedBytes(DTO):
    job_id: UUID
    fence_token: int
    manifest_hash: str
    format: Literal['pdf','docx']
    mime_type: str
    plaintext_sha256: str
    bytes: bytes
class StagedArtifact(DTO):
    artifact_id: UUID
    job_id: UUID
    fence_token: int
    manifest_id: UUID
    document_kind: Literal['purchase_card','application','product_card','checklist']
    format: Literal['pdf','docx']
    plaintext_sha256: str
    encrypted_blob_ref: str
    size_bytes: int = Field(ge=0)
    created_at: datetime
class ArtifactRecord(StagedArtifact):
    manifest_hash: str
    owner_id: UUID
    case_id: UUID
    case_revision: int
    deletion_epoch: int
    publication_status: Literal['published','expired','deleted'] = 'published'
    published_at: datetime
    expires_at: datetime
class MaterialPermit(DTO):
    owner_id: UUID
    case_id: UUID
    current_case_guard: CaseGuard
    artifact_id: UUID
    artifact_original_revision: int
    disposition: Literal['current','historical']
    warning_acknowledged: bool
    expires_at: datetime
class EncryptedBlob(DTO):
    key_id: str
    nonce: bytes
    ciphertext: bytes
    tag: bytes


class WorkRef(DTO):
    job_id: UUID
    kind: Literal['process_inbox','render_artifact','deliver_outbox']
    payload_ref: UUID
    owner_id: UUID
    case_id: UUID | None = None
    case_revision: int | None = None
    deletion_epoch: int = 0
    dedupe_key: str
    schema_version: str = '1.0.0'
class ClaimedJob(WorkRef):
    fence_token: int = Field(ge=1)
    lease_owner: str
    lease_until: datetime
    attempt: int = Field(ge=1)
    next_attempt_at: datetime
    trace_id: UUID
class RenderPayload(DTO):
    manifest_id: UUID
    bundle_id: UUID
    artifact_spec: ArtifactSpec
class DeliveryIntent(DTO):
    outbox_id: UUID
    owner_id: UUID
    delivery_target_id: UUID
    case_guard: CaseGuard | None = None
    kind: Literal['view','material','callback_answer']
    view_ref: UUID | None = None
    material_permit_ref: UUID | None = None
    callback_answer_ref: UUID | None = None
    dedupe_key: str
    created_at: datetime
    @model_validator(mode='after')
    def one_ref(self):
        refs={'view':self.view_ref,'material':self.material_permit_ref,'callback_answer':self.callback_answer_ref}
        if refs[self.kind] is None or sum(v is not None for v in refs.values())!=1: raise ValueError('exactly one delivery reference required')
        if self.kind=='material' and self.case_guard is None: raise ValueError('material delivery requires guard')
        return self
class SendPermit(DTO):
    outbox_id: UUID
    send_attempt_id: UUID
    job_id: UUID
    fence_token: int
    owner_id: UUID
    delivery_target_id: UUID
    case_guard: CaseGuard | None = None
    approved_at: datetime
    payload_hash: str
    expires_at: datetime
class MessageReceipt(DTO):
    operation: Literal['send_message','edit_message']
    message_id: str
    accepted_at: datetime
class CallbackReceipt(DTO):
    operation: Literal['answer_callback'] = 'answer_callback'
    callback_id: str
    acknowledged_at: datetime
class TransportResult(DTO):
    status: Literal['confirmed','definitely_rejected','unknown']
    receipt: MessageReceipt | CallbackReceipt | None = None
    error_code: str | None = None
    retry_after: float | None = None
    retryable: bool = False
    reason_code: str | None = None
    @model_validator(mode='after')
    def transport_state(self):
        if self.status=='confirmed' and (self.receipt is None or self.error_code or self.reason_code or self.retryable): raise ValueError('confirmed requires receipt only')
        if self.status=='definitely_rejected' and (not self.error_code or self.receipt is not None or self.reason_code): raise ValueError('rejection requires error')
        if self.status=='unknown' and (not self.reason_code or self.receipt is not None or self.retryable or self.error_code): raise ValueError('unknown outcome cannot be safe retry')
        return self
class UploadResult(DTO):
    attachment_token_ref: UUID
    artifact_id: UUID
    owner_id: UUID
    manifest_hash: str
    state: Literal['uploaded','processing','ready','unknown']
    observed_at: datetime
class AttachmentRecord(UploadResult):
    token: str
AttachmentTokenRecord = AttachmentRecord
class DeleteReceipt(DTO):
    case_id: UUID
    deletion_epoch: int
    own_access_revoked_at: datetime
    cleanup: Literal['pending','completed']
    inflight_delivery_possible: bool
class RetryDecision(DTO):
    mode: Literal['none','safe','explicit_user']
    next_attempt_at: datetime | None = None
    reason_code: str
class ShutdownReport(DTO):
    finished_ids: tuple[UUID,...] = ()
    released_safe_ids: tuple[UUID,...] = ()
    unknown_send_ids: tuple[UUID,...] = ()


class InboxRecord(DTO):
    inbox_id: UUID
    owner_id: UUID
    bot_scope: str
    event_key: str
    event: NormalizedEvent
    status: Literal['received','processing','processed','ignored','failed'] = 'received'
    received_at: datetime
class IdentityRecord(DTO):
    identity_id: UUID
    owner_id: UUID
    delivery_target_id: UUID
    bot_scope: str
    lookup_key: str
    platform_user_id: str | None = None
    platform_chat_id: str | None = None
class HandleRecord(DTO):
    handle_id: UUID
    handle: str
    owner_id: UUID
    case_guard: CaseGuard | None = None
    dialog_revision: int | None = None
    action: ActionIntent
    expires_at: datetime
CallbackHandle = HandleRecord
class JobRecord(DTO):
    work: WorkRef
    status: Literal['queued','running','succeeded','failed','cancelled','retry_wait'] = 'queued'
    fence_token: int = 0
    lease_owner: str | None = None
    lease_until: datetime | None = None
    attempt: int = 0
    next_attempt_at: datetime
    trace_id: UUID
    bot_scope: str = 'default'
    render_payload: RenderPayload | None = None
class OutboxRecord(DTO):
    outbox_id: UUID
    owner_id: UUID
    intent: DeliveryIntent
    status: Literal['pending','preparing','sending','confirmed','definitely_rejected','delivery_unknown','retry_wait'] = 'pending'
    payload: ViewModel | CallbackAnswer | MaterialPermit | None = None
    send_permit: SendPermit | None = None
    transport_result: TransportResult | None = None


class FieldError(DTO):
    path: str
    code: str
    safe_message_key: str
class ValidationReport(DTO):
    valid: bool
    errors: tuple[FieldError,...] = ()
    warnings: tuple[str,...] = ()
class ImportReport(DTO):
    release_ref: VersionRef
    status: Literal['valid','rejected','staged']
    files: tuple[str,...] = ()
    errors: tuple[FieldError,...] = ()
    warnings: tuple[str,...] = ()
class ActivationReceipt(DTO):
    previous_ref: VersionRef | None = None
    active_ref: VersionRef
    activated_at: datetime
    actor_key: str
class RecoveryReport(DTO):
    exhausted_ids: tuple[UUID,...] = ()
    requeued_ids: tuple[UUID,...] = ()
    unknown_delivery_ids: tuple[UUID,...] = ()
    cancelled_ids: tuple[UUID,...] = ()
    stale_claims: tuple[UUID,...] = ()
class DeleteReport(DTO):
    removed_count: int = 0
    remaining_count: int = 0
    errors: tuple[str,...] = ()
class HealthSnapshot(DTO):
    status: str
    release_commit: str
    db_ready: bool
    worker_heartbeat_age: float | None = None
    oldest_job_age: float | None = None
    counts: Mapping[str,int] = Field(default_factory=dict)
class SubscriptionHealth(DTO):
    status: Literal['active','missing','unreachable']
    checked_at: datetime
    reason_code: str | None = None
class AuditEvent(DTO):
    event_id: UUID
    kind: str
    internal_subject_id: UUID
    actor_key: str
    timestamp: datetime
    result_code: str
class LifecycleReceipt(DTO):
    ref: VersionRef
    status: str
    changed_at: datetime
    actor_key: str
class BackupReceipt(DTO):
    backup_id: UUID
    created_at: datetime
    encrypted_location: str
    manifest_hash: str
class RestoreReport(DTO):
    restored_version: str
    consistency_errors: tuple[str,...] = ()
    deletions_applied: int
    ready: bool
class CommandBatch(DTO):
    event: NormalizedEvent
    command: CommandEnvelope | None = None
    ignored: bool = False

class ComparisonRecord(DTO):
    comparison_id: UUID
    owner_id: UUID
    case_id: UUID
    case_guard: CaseGuard
    result: ComparisonResult
    quote: PricingResult

class UploadPermit(DTO):
    artifact_id: UUID
    owner_id: UUID
    manifest_hash: str
    approved_at: datetime
    expires_at: datetime

class StagingManifestRef(DTO):
    manifest_id: UUID
    manifest_hash: str
    document_kind: Literal['purchase_card','application','product_card','checklist']

class RenderJobContext(DTO):
    manifest: DocumentManifest
    payload: RenderPayload

class MaterialDeliveryContext(DTO):
    permit: MaterialPermit
    artifact: ArtifactRecord
    upload_permit: UploadPermit

UploadedAttachmentRecord = AttachmentRecord

class CatalogPack(DTO):
    schema_version: str = '1.0.0'
    catalog_id: str
    version: str
    data_kind: Literal['synthetic','public_snapshot']
    review: Review
    suppliers: tuple[Supplier,...]
    offers: tuple[OfferSnapshot,...]
    @property
    def ref(self): return VersionRef(id=self.catalog_id,version=self.version)

class SourcesRegistry(DTO):
    schema_version: str = '1.0.0'
    registry_id: str
    version: str
    data_kind: Literal['synthetic','public_snapshot']
    review: Review
    sources: tuple[SourceRecord,...]
    @property
    def ref(self): return VersionRef(id=self.registry_id,version=self.version)

class TemplateAsset(DTO):
    format: Literal['pdf','docx']
    renderer_id: str
    path: str
    sha256: str | None = None

class TemplateEntry(DTO):
    template_id: str
    version: str
    document_kind: Literal['purchase_card','application','product_card','checklist']
    assets: tuple[TemplateAsset,...]
    @property
    def ref(self): return VersionRef(id=self.template_id,version=self.version)

class TemplateRegistry(DTO):
    schema_version: str = '1.0.0'
    registry_id: str
    version: str
    data_kind: Literal['synthetic','public_snapshot']
    review: Review
    templates: tuple[TemplateEntry,...]
    @property
    def ref(self): return VersionRef(id=self.registry_id,version=self.version)

class ReleaseFile(DTO):
    kind: str
    path: str
    sha256: str
    ref: VersionRef

class ReleasePackManifest(DTO):
    schema_version: str = '1.0.0'
    release_id: str
    version: str
    data_kind: Literal['synthetic','public_snapshot']
    review: Review
    files: tuple[ReleaseFile,...]
    @property
    def ref(self): return VersionRef(id=self.release_id,version=self.version)

class DemoProfile(DTO):
    profile_id: str
    is_synthetic: Literal[True] = True
    category_id: str
    profile_ref: VersionRef
    region_code: str | None = None
    role: Literal['self','representative']
    prescribed: Mapping[str,RequirementValue] = Field(default_factory=dict)
    unspecified_fields: tuple[str,...] = ()
    certificate_amount: Money | None = None
    certificate_applicable_declared: Literal['yes','no','unknown'] = 'unknown'
    applicant_status_declared: Literal['yes','no','unknown'] = 'unknown'
    route_answers: Mapping[str,Literal['yes','no','unknown']] = Field(default_factory=dict)
    document_fields: Mapping[str,str] = Field(default_factory=dict)

class DemoCasePack(DTO):
    schema_version: str = '1.0.0'
    pack_id: str
    version: str
    data_kind: Literal['synthetic'] = 'synthetic'
    review: Review
    profiles: tuple[DemoProfile,...]

class QuoteLine(DTO):
    message_key: str
    parameters: SafeParameters = Field(default_factory=dict)

class QuoteExplanation(DTO):
    lines: tuple[QuoteLine,...]
    warnings: tuple[str,...] = ()

class EvidenceExplanation(DTO):
    evidence: tuple[Evidence,...]
    sources: tuple[SourceRecord,...]

class OrderedComparisonRefs(DTO):
    comparison_ids: tuple[UUID,...]
    reason_keys: tuple[str,...] = ()

class DemoRelease(DTO):
    profile: CategoryProfile
    offers: tuple[OfferSnapshot,...]
    route: RoutePack
    sources: SourcesRegistry
    template_refs: tuple[VersionRef,...]
    release_ref: VersionRef
    catalog_ref: VersionRef
    route_lifecycle: LifecycleRecord
    manifest: ReleasePackManifest
    catalog: CatalogPack
    templates: TemplateRegistry
    demo_profiles: tuple[DemoProfile,...] = ()

class ReleaseRecord(DTO):
    ref: VersionRef
    manifest: ReleasePackManifest
    validated_at: datetime
    package_reviews: tuple[Review,...] = ()
    package_data_kinds: tuple[str,...] = ()
    assets_verified: bool = False
    content_hash: str

class ReleaseLifecycle(DTO):
    ref: VersionRef
    revoked: bool = False
    review_due_at: datetime | None = None
    reason_code: str | None = None

class CaseCleanupRecord(DTO):
    case_id: UUID
    deletion_epoch: int

class CommandDraft(DTO):
    type: CommandType
    payload: CommandPayload
    required_guard: CaseGuard | DialogGuard | None = None
    @model_validator(mode='after')
    def payload_matches(self):
        if not isinstance(self.payload,COMMAND_PAYLOADS[self.type]): raise ValueError('command draft payload mismatch')
        return self

class StepDecision(DTO):
    step: Literal['start','input','comparison','selection','branch','route','review','preparing','materials']
    required_fields: tuple[str,...] = ()
    available_actions: tuple[str,...] = ()
