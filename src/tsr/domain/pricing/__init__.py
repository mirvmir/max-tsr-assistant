"""Integer kopek calculations; declared conditions never imply external approval."""
from decimal import Decimal, InvalidOperation
import re

from tsr.contracts import CalculatedEvidence, Money, PricingResult, Result, VersionRef

PRICING_ALGORITHM = VersionRef(id='pricing',version='1.0.0')
INPUT_MAX = 10_000_000_000
MONEY_MAX = 9_000_000_000_000_000


def validate_money_input(raw_text,locale='ru'):
    raw = raw_text.strip()
    if locale.startswith('ru'):
        raw=raw.replace(',', '.')
    if not re.fullmatch(r'\d+(?:\.\d{1,2})?',raw):
        return Result.failure('VALIDATION_ERROR',safe_message_key='money.invalid_precision')
    try:
        amount = int(Decimal(raw)*100)
    except (ValueError,InvalidOperation,OverflowError):
        return Result.failure('VALIDATION_ERROR')
    if amount > INPUT_MAX:
        return Result.failure('VALIDATION_ERROR',safe_message_key='money.input_limit')
    return Result.success(Money(minor=amount))


def _amount(fact):
    if fact.status == 'known' and fact.value.kind == 'money':
        return fact.value.value
    return None


def quote_purchase(input_revision,snapshot,algorithm_ref):
    if algorithm_ref.version != '1.0.0' or algorithm_ref.id not in ('pricing','pricing-v1'):
        return Result.failure('VALIDATION_ERROR',field_key='algorithm_ref')
    if input_revision.profile_ref != snapshot.profile_ref:
        return Result.failure('INCOMPATIBLE_PROFILE')
    price = _amount(snapshot.price) if snapshot.price_kind == 'exact' else None
    certificate = input_revision.certificate_amount
    charge = _amount(snapshot.delivery.charge)
    if any(value is not None and not 0 <= value.minor <= INPUT_MAX for value in (price,certificate,charge)):
        return Result.failure('VALIDATION_ERROR',field_key='money')
    reasons, assumptions = [], []
    applicable = input_revision.certificate_applicable_declared
    acceptance = snapshot.accepts_certificate
    accepted = acceptance.value.value if acceptance.status == 'known' and acceptance.value.kind == 'boolean' else None
    if applicable == 'no':
        reasons.append('certificate.not_applicable')
    if accepted is False:
        reasons.append('certificate.not_accepted')
    use = 'not_applicable' if applicable == 'no' else 'not_accepted' if accepted is False else 'allowed_by_declared_data' if applicable == 'yes' and accepted is True else 'conditional' if price is not None and certificate is not None else 'unknown'
    coverage = gap = total = None
    if price is not None:
        if use in ('not_applicable','not_accepted'):
            coverage, gap = Money(minor=0),price
        elif certificate is not None:
            coverage=Money(minor=min(price.minor,certificate.minor))
            gap=Money(minor=max(price.minor-certificate.minor,0))
    else:
        reasons.append('price.not_exact')
    if gap is not None:
        if snapshot.delivery.mode == 'included' and charge is not None and charge.minor == 0:
            total=gap
        elif snapshot.delivery.mode == 'separate' and charge is not None:
            combined=gap.minor+charge.minor
            if combined > MONEY_MAX:
                return Result.failure('VALIDATION_ERROR',field_key='possible_own_total')
            total=Money(minor=combined)
            assumptions.append('delivery.paid_by_user')
        else:
            reasons.append('delivery.not_known')
    if use == 'conditional':
        reasons.append('certificate.conditions_not_confirmed')
        assumptions.append('certificate.applicable_and_accepted')
    status='incomplete' if gap is None or total is None else 'conditional' if use == 'conditional' or assumptions else 'calculated'
    return Result.success(PricingResult(pricing_algorithm_ref=algorithm_ref,input_revision_id=input_revision.input_revision_id,
        snapshot_id=snapshot.snapshot_id,price=price,certificate_limit=certificate,certificate_use=use,
        coverage=coverage,gap=gap,delivery=snapshot.delivery,possible_own_total=total,status=status,
        reasons=tuple(reasons),assumptions=tuple(assumptions),provenance=CalculatedEvidence(algorithm=algorithm_ref,
        input_refs=(str(input_revision.input_revision_id),str(snapshot.snapshot_id)))))


def describe_quote(result):
    from tsr.contracts import QuoteExplanation, QuoteLine, SafeParameters
    lines=[]
    for key in ('price','certificate_limit','coverage','gap','possible_own_total'):
        value=getattr(result,key)
        lines.append(QuoteLine(message_key='quote.'+key,parameters={'amount':value if value is not None else None}))
    return QuoteExplanation(lines=tuple(lines),warnings=tuple(result.reasons)+tuple(result.assumptions))
