"""Deterministic comparisons against an exact versioned category profile."""
from decimal import Decimal
import unicodedata

from tsr.contracts import (ComparisonResult, Fact, FieldMatch, OrderedComparisonRefs,
    Result, SupplierQuestion, VersionRef)

MATCHING_ALGORITHM = VersionRef(id='matching', version='1.0.0')


def _equal(left, right, tolerance=None):
    if left.kind != right.kind:
        return False
    if left.kind == 'quantity':
        if left.unit != right.unit:
            raise ValueError('unit')
        return abs(Decimal(left.value)-Decimal(right.value)) <= Decimal(tolerance or '0')
    if left.kind == 'text':
        return unicodedata.normalize('NFC', left.value) == unicodedata.normalize('NFC', right.value)
    return left.value == right.value


def _matches(required, actual, rule):
    if rule.operator == 'eq':
        return _equal(required, actual, rule.tolerance)
    if rule.operator in ('gte', 'lte'):
        if required.kind != 'quantity' or actual.kind != 'quantity' or required.unit != actual.unit:
            raise ValueError('quantity')
        return Decimal(actual.value) >= Decimal(required.value) if rule.operator == 'gte' else Decimal(actual.value) <= Decimal(required.value)
    if rule.operator == 'within_range':
        if required.kind != 'range' or actual.kind != 'quantity' or required.unit != actual.unit:
            raise ValueError('range')
        return Decimal(required.minimum) <= Decimal(actual.value) <= Decimal(required.maximum)
    if rule.operator == 'in_set':
        if required.kind != 'set':
            raise ValueError('set')
        return any(_equal(item, actual) for item in required.values)
    raise ValueError('operator')


def compare_offer(comparison_id, input_revision, snapshot, profile, algorithm_ref, now):
    ref = VersionRef(id=profile.profile_id,version=profile.version)
    if input_revision.profile_ref != ref or snapshot.profile_ref != ref or input_revision.category_id != profile.category_id or snapshot.variant.category_id != profile.category_id:
        return Result.failure('INCOMPATIBLE_PROFILE')
    if algorithm_ref.version != '1.0.0' or algorithm_ref.id not in ('matching','matching-v1'):
        return Result.failure('VALIDATION_ERROR',field_key='algorithm_ref')
    if set(input_revision.prescribed) & set(input_revision.unspecified_fields):
        return Result.failure('VALIDATION_ERROR',field_key='prescribed')
    rule_keys={rule.field_key for rule in profile.fields}
    if set(input_revision.prescribed)-rule_keys or set(snapshot.attributes)-rule_keys:
        return Result.failure('VALIDATION_ERROR',field_key='attributes')
    fields, questions = [], []
    for rule in profile.fields:
        required = input_revision.prescribed.get(rule.field_key)
        fact = snapshot.attributes.get(rule.field_key,Fact.unknown('missing_attribute'))
        question = None
        if required is not None:
            expected_kind='range' if rule.operator=='within_range' else 'set' if rule.operator=='in_set' else rule.value_kind
            if required.kind!=expected_kind or required.kind in ('quantity','range') and required.unit!=rule.unit:
                return Result.failure('VALIDATION_ERROR',field_key=rule.field_key)
            if required.kind=='set' and any(item.kind!=rule.value_kind or item.kind=='code' and rule.allowed_codes and item.value not in rule.allowed_codes for item in required.values):
                return Result.failure('VALIDATION_ERROR',field_key=rule.field_key)
        known_facts=(fact,) if fact.status=='known' else fact.alternatives
        if any(item.value.kind!=rule.value_kind or item.value.kind=='quantity' and item.value.unit!=rule.unit for item in known_facts):
            return Result.failure('VALIDATION_ERROR',field_key=rule.field_key)
        if required is None:
            status = 'unspecified_input'
        elif fact.status != 'known':
            status = 'unknown_offer'
            question = SupplierQuestion(question_key='parameter.'+rule.field_key,field_key=rule.field_key,
                text_key='supplier.parameter',source_refs=tuple(snapshot.source_ids),category='parameter')
            questions.append(question)
        else:
            try:
                status = 'match' if _matches(required,fact.value,rule) else 'mismatch'
            except (ValueError, TypeError, ArithmeticError):
                return Result.failure('VALIDATION_ERROR',field_key=rule.field_key)
        fields.append(FieldMatch(field_key=rule.field_key,status=status,
            required_for_complete=rule.required_for_complete_comparison,required_value=required,
            offer_fact=fact,reason_code=status,evidence_refs=tuple(item.source_id if item.kind=='source' else str(item.input_revision_id) if item.kind=='user' else item.algorithm.id for item in fact.evidence),supplier_question=question))
    classification = 'complete'
    if any(field.status == 'mismatch' for field in fields):
        classification = 'mismatch'
    elif not any(field.required_for_complete and field.required_value is not None for field in fields) or any(field.required_for_complete and field.status != 'match' for field in fields):
        classification = 'incomplete'
    return Result.success(ComparisonResult(comparison_id=comparison_id,input_revision_id=input_revision.input_revision_id,
        snapshot_id=snapshot.snapshot_id,profile_ref=ref,matching_algorithm_ref=algorithm_ref,
        computed_at=now,fields=tuple(fields),classification=classification,questions=tuple(questions)))


def compare_many(comparison_ids_by_snapshot,input_revision,snapshots,profile,algorithm_ref,now):
    if len(snapshots)>20 or any(snapshot.snapshot_id not in comparison_ids_by_snapshot for snapshot in snapshots):
        return Result.failure('VALIDATION_ERROR')
    values=[]
    for snapshot in snapshots:
        result=compare_offer(comparison_ids_by_snapshot[snapshot.snapshot_id],input_revision,snapshot,profile,algorithm_ref,now)
        if not result.ok:
            return result
        values.append(result.value)
    return Result.success(tuple(values))


def rank_comparisons(comparisons,quotes,sort_mode='completeness'):
    quotes_by_id={quote.snapshot_id:quote for quote in quotes}
    def key(comparison):
        quote=quotes_by_id.get(comparison.snapshot_id)
        price=quote.price.minor if quote and quote.price is not None else 9000000000000001
        return ({'complete':0,'incomplete':1,'mismatch':2}[comparison.classification],price,str(comparison.snapshot_id))
    ordered=sorted(comparisons,key=key)
    return OrderedComparisonRefs(comparison_ids=tuple(item.comparison_id for item in ordered),
        reason_keys=('ranking.completeness_first','ranking.price_exact_only'))


def supplier_questions(comparison,quote,route_eval=None):
    questions=list(comparison.questions)
    if quote.price is None:
        questions.append(SupplierQuestion(question_key='price.exact',field_key=None,text_key='supplier.price',source_refs=(),category='price'))
    if quote.possible_own_total is None:
        questions.append(SupplierQuestion(question_key='delivery.charge',field_key=None,text_key='supplier.delivery',source_refs=(),category='delivery'))
    if quote.certificate_use in ('conditional','unknown'):
        questions.append(SupplierQuestion(question_key='certificate.acceptance',field_key=None,text_key='supplier.certificate',source_refs=(),category='certificate'))
    if route_eval and any(item.condition_id == 'fund_contract' and item.status == 'unknown' for item in route_eval.conditions):
        questions.append(SupplierQuestion(question_key='fund.contract',field_key=None,text_key='supplier.fund_contract',source_refs=(),category='fund_contract'))
    return tuple({question.question_key:question for question in questions}.values())
