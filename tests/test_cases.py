from datetime import datetime, timedelta, timezone
from uuid import UUID
import pytest

from tsr.contracts import ActorContext, AttributeRule, CategoryProfile, ErrorCode, Review, UnknownValue, MoneyValue, Money
from tsr.domain.cases import initialize_input, create_case, propose_value, confirm_value, choose_branch, authorize_owned, mark_case_deleted

NOW=datetime(2026,9,29,tzinfo=timezone.utc)
def uid(n): return UUID(int=n)

def setup_case():
    ctx=ActorContext(owner_id=uid(1),delivery_target_id=uid(2),bot_scope='demo',case_mode='demo',correlation_id=uid(3))
    field=AttributeRule(field_key='width',label='Width',value_kind='quantity',unit='mm',operator='eq',required_for_complete_comparison=True)
    profile=CategoryProfile(profile_id='wheelchair',version='1.0.0',category_id='wheelchair',data_kind='synthetic',review=Review(),fields=(field,))
    initial=initialize_input(uid(5),ctx,uid(4),profile,'representative',NOW)
    case=create_case(ctx,uid(4),profile.ref,initial,NOW)
    return ctx,field,initial,case


def test_old_candidate_button_cannot_confirm_replacement_and_immutability():
    ctx,field,initial,case=setup_case()
    first=propose_value(case,uid(6),field,'420',NOW).value
    case=case.model_copy(update={'dialog_revision':first.dialog_revision})
    second=propose_value(case,uid(7),field,'440',NOW).value
    case=case.model_copy(update={'dialog_revision':second.dialog_revision})
    stale=confirm_value(case,uid(8),first,first.value_hash,initial,NOW)
    assert not stale.ok and stale.error.code==ErrorCode.STALE_CANDIDATE
    fresh=confirm_value(case,uid(8),second,second.value_hash,initial,NOW)
    assert fresh.ok and fresh.value.new_input_revision.prescribed['width'].value=='440'
    assert initial.prescribed=={} and fresh.value.new_case.case_revision==case.case_revision+1
    with pytest.raises(TypeError): fresh.value.new_input_revision.prescribed['width']='changed'


def test_unknown_scalar_amount_and_delete_access_fail_closed():
    ctx,field,initial,case=setup_case()
    unknown=propose_value(case,uid(6),field,UnknownValue(),NOW).value
    case=case.model_copy(update={'dialog_revision':unknown.dialog_revision})
    mutation=confirm_value(case,uid(8),unknown,unknown.value_hash,initial,NOW).value
    assert mutation.new_input_revision.unspecified_fields==('width',)
    assert 'width' not in mutation.new_input_revision.prescribed
    amount=AttributeRule(field_key='certificate_amount',label='Amount',value_kind='money')
    bad=propose_value(mutation.new_case,uid(9),amount,'1.001',NOW)
    assert not bad.ok and bad.error.code==ErrorCode.VALIDATION_ERROR
    good=propose_value(mutation.new_case,uid(9),amount,'0',NOW).value
    current=mutation.new_case.model_copy(update={'dialog_revision':good.dialog_revision})
    changed=confirm_value(current,uid(10),good,good.value_hash,mutation.new_input_revision,NOW).value
    assert changed.new_input_revision.certificate_amount.minor==0
    assert 'certificate_amount' not in changed.new_input_revision.prescribed
    foreign=ctx.model_copy(update={'owner_id':uid(100)})
    assert authorize_owned(foreign,current.owner_id,current.guard,current).error.code==ErrorCode.ACCESS_DENIED
    deletion=mark_case_deleted(current,NOW)
    dead=current.model_copy(update={'status':'deleted','deletion_epoch':deletion.new_epoch})
    assert authorize_owned(ctx,dead.owner_id,dead.guard,dead).error.code==ErrorCode.CASE_DELETED
    assert not choose_branch(dead,'purchase').ok


def test_navigation_revision_and_branch_changes_invalidate_confirmation():
    _,_,initial,case=setup_case()
    displayed=case.model_copy(update={'dialog_revision':case.dialog_revision+4,'active_confirmation_id':uid(99)})
    assert displayed.case_revision==case.case_revision
    branch=choose_branch(displayed,'purchase').value
    assert branch.new_case.case_revision==case.case_revision+1
    assert branch.new_case.active_confirmation_id is None
    assert branch.invalidation.clear_confirmation and branch.invalidation.cancel_old_jobs
