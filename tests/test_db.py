"""Real PostgreSQL invariants; deleting a guard or CAS predicate breaks these."""
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest


@pytest.fixture
def db():
    from tsr.adapters.db import Database
    from tsr.adapters.crypto import Crypto
    dsn = os.getenv('TSR_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('TSR_TEST_DATABASE_URL required for real PostgreSQL integration')
    import psycopg
    from psycopg.conninfo import make_conninfo
    schema='test_db_'+uuid4().hex
    with psycopg.connect(dsn) as connection:
        connection.execute(psycopg.sql.SQL('CREATE SCHEMA {}').format(psycopg.sql.Identifier(schema)))
    instance = Database(make_conninfo(dsn,options='-c search_path='+schema), Crypto(b'x' * 32))
    instance.migrate()
    try:
        yield instance
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(psycopg.sql.SQL('DROP SCHEMA {} CASCADE').format(psycopg.sql.Identifier(schema)))


def test_explicit_commit_rollback_owner_and_encrypted_payload(db):
    from tsr.contracts import IdentityRecord, ActorContext
    owner, target = uuid4(), uuid4()
    record = IdentityRecord(identity_id=uuid4(), owner_id=owner, delivery_target_id=target,
                            bot_scope='test',lookup_key=str(uuid4()), platform_chat_id='sensitive-chat')
    with db.uow() as u:
        u.identities.insert(record)
    with db.uow() as u:
        assert u.identities.get(record.identity_id) is None
        u.identities.insert(record)
        u.commit()
    foreign = ActorContext(owner_id=uuid4(),delivery_target_id=target,bot_scope='test',case_mode='demo',correlation_id=uuid4())
    with db.uow() as u:
        assert u.identities.get(record.identity_id) == record
        assert not u.identities.get_owned(foreign,record.identity_id).ok
        row = u.connection.execute('SELECT ciphertext FROM user_identities WHERE id=%s',(record.identity_id,)).fetchone()
        assert b'sensitive-chat' not in bytes(row['ciphertext'])


def test_scoped_job_dedupe_takeover_and_stale_fence_cas(db):
    from tsr.contracts import WorkRef, NormalizedEvent, StartEvent, InboxRecord
    now = datetime.now(timezone.utc)
    owner, target, inbox_id = uuid4(),uuid4(),uuid4()
    event = NormalizedEvent(inbox_id=inbox_id,bot_scope='test',dedupe_key=str(uuid4()),owner_id=owner,
                            delivery_target_id=target,kind='start',occurred_at=now,received_at=now,payload=StartEvent())
    work = WorkRef(job_id=uuid4(),kind='process_inbox',payload_ref=inbox_id,owner_id=owner,
                   case_id=None,case_revision=None,deletion_epoch=0,dedupe_key=str(uuid4()),schema_version='1.0.0')
    with db.uow() as u:
        u.inbox.insert(InboxRecord(inbox_id=inbox_id,owner_id=owner,bot_scope='test',event_key=event.dedupe_key,event=event,received_at=now))
        assert u.work.enqueue_unique(work) == work
        assert u.work.enqueue_unique(work.model_copy(update={'job_id':uuid4()})) == work
        u.commit()
    now=datetime.now(timezone.utc)
    with db.uow() as u:
        first=u.work.claim_next('process_inbox','first',now,1)
        assert first.job_id == work.job_id
        u.commit()
    with db.uow() as u:
        later=now+timedelta(seconds=2)
        u.work.recover_expired(later)
        second=u.work.claim_next('process_inbox','second',later,30)
        assert second.fence_token > first.fence_token
        assert not u.work.finish_if_claim(first,later)
        assert u.work.finish_if_claim(second,later)
        u.commit()


def test_expired_sending_becomes_unknown_without_safe_retry(db):
    from tsr.contracts import WorkRef, OutboxRecord, DeliveryIntent, SendPermit, content_hash
    now = datetime.now(timezone.utc)
    owner,target,outbox_id=uuid4(),uuid4(),uuid4()
    intent=DeliveryIntent(outbox_id=outbox_id,owner_id=owner,delivery_target_id=target,case_guard=None,
        kind='view',view_ref=uuid4(),material_permit_ref=None,callback_answer_ref=None,dedupe_key=str(uuid4()),created_at=now)
    work=WorkRef(job_id=uuid4(),kind='deliver_outbox',payload_ref=outbox_id,owner_id=owner,case_id=None,
        case_revision=None,deletion_epoch=0,dedupe_key=str(uuid4()),schema_version='1.0.0')
    with db.uow() as u:
        u.outbox.append_unique(OutboxRecord(outbox_id=outbox_id,owner_id=owner,intent=intent,status='pending'))
        u.work.enqueue_unique(work)
        u.commit()
    now=datetime.now(timezone.utc)
    with db.uow() as u:
        claim=u.work.claim_next('deliver_outbox','sender',now,1)
        permit=SendPermit(outbox_id=outbox_id,send_attempt_id=uuid4(),job_id=claim.job_id,fence_token=claim.fence_token,
            owner_id=owner,delivery_target_id=target,case_guard=None,approved_at=now,payload_hash=content_hash(None),expires_at=now+timedelta(seconds=1))
        assert u.outbox.begin_send_if_allowed(claim,permit,now).ok
        u.commit()
    with db.uow() as u:
        later=now+timedelta(seconds=2)
        assert not u.outbox.recover_sending(later,bot_scope='another-bot').unknown_delivery_ids
        assert u.outbox.get(outbox_id).status=='sending'
        assert u.work.get(work.job_id).status=='running'
        report=u.work.recover_expired(later,max_attempts=1,bot_scope='default')
        assert not report.exhausted_ids
        assert outbox_id in report.unknown_delivery_ids
        assert u.outbox.get(outbox_id).status=='delivery_unknown'
        assert u.work.get(work.job_id).status=='failed'
        assert u.work.claim_next('deliver_outbox','replacement',later,30) is None
        u.commit()


def test_tombstone_cleanup_fenced_and_clears_sensitive_ciphertext(db):
    from tsr.contracts import ActorContext, CaseSnapshot, InputRevision, VersionRef
    now=datetime.now(timezone.utc)
    ctx=ActorContext(owner_id=uuid4(),delivery_target_id=uuid4(),bot_scope='test',case_mode='demo',correlation_id=uuid4())
    case_id,input_id=uuid4(),uuid4()
    ref=VersionRef(id='test-profile',version='1.0.0')
    revision=InputRevision(input_revision_id=input_id,owner_id=ctx.owner_id,case_id=case_id,created_at=now,
        category_id='test',profile_ref=ref,role='self',document_fields={'name':'sensitive name'})
    case=CaseSnapshot(case_id=case_id,owner_id=ctx.owner_id,mode='demo',category_id='test',profile_ref=ref,
        input_revision_id=input_id,last_activity_at=now)
    with db.uow() as u:
        u.inputs.insert(revision)
        u.cases.insert(case)
        u.commit()
    with db.uow() as u:
        assert u.cases.mark_deleted(ctx,case.guard,now).ok
        u.commit()
    assert db.list_cleanup_requests()[0].case_id==case_id
    assert not db.complete_case_cleanup(case_id,0)
    assert db.complete_case_cleanup(case_id,1)
    assert db.complete_case_cleanup(case_id,1)
    with db.uow() as u:
        assert not u.cases.get_owned(ctx,case_id).ok
        assert u.inputs.get(input_id) is None
        row=u.connection.execute('SELECT ciphertext FROM input_revisions WHERE id=%s',(input_id,)).fetchone()
        assert bytes(row['ciphertext'])==b''


def test_scoped_claim_cannot_take_another_bot_job(db):
    from tsr.contracts import WorkRef, NormalizedEvent, StartEvent, InboxRecord
    now=datetime.now(timezone.utc)
    ids={}
    with db.uow() as u:
        for scope in ('production','local-demo'):
            owner,target,inbox_id=uuid4(),uuid4(),uuid4()
            event=NormalizedEvent(inbox_id=inbox_id,bot_scope=scope,dedupe_key=str(uuid4()),owner_id=owner,
                delivery_target_id=target,kind='start',occurred_at=now,received_at=now,payload=StartEvent())
            u.inbox.insert(InboxRecord(inbox_id=inbox_id,owner_id=owner,bot_scope=scope,event_key=event.dedupe_key,event=event,received_at=now))
            work=WorkRef(job_id=uuid4(),kind='process_inbox',payload_ref=inbox_id,owner_id=owner,
                dedupe_key=str(uuid4()),schema_version='1.0.0')
            u.work.enqueue_unique(work)
            ids[scope]=work.job_id
        u.commit()
    now=datetime.now(timezone.utc)
    with db.uow() as u:
        claim=u.work.claim_next('process_inbox','demo-worker',now,30,bot_scope='local-demo')
        assert claim.job_id==ids['local-demo']
        assert u.work.claim_next('process_inbox','demo-worker',now,30,bot_scope='local-demo') is None
        assert u.work.get(ids['production']).status=='queued'
        production=u.work.claim_next('process_inbox','production-worker',now,30,bot_scope='production')
        assert production.job_id==ids['production']
        u.commit()


@pytest.mark.parametrize('intent_kind,invalid_receipt',[
    ('callback_answer','message'),('callback_answer','foreign_callback'),
    ('view','callback'),('view','edit'),('view','valid'),('callback_answer','valid'),
])
def test_confirmed_receipt_must_match_persisted_delivery(db,intent_kind,invalid_receipt):
    from tsr.contracts import (WorkRef,OutboxRecord,DeliveryIntent,SendPermit,TransportResult,
        CallbackAnswer,CallbackReceipt,MessageReceipt,ViewModel,content_hash)
    now=datetime.now(timezone.utc)
    owner,target,outbox_id=uuid4(),uuid4(),uuid4()
    payload=(CallbackAnswer(owner_id=owner,platform_callback_id='verified-callback',text_key='text',inbox_id=uuid4())
        if intent_kind=='callback_answer' else ViewModel(view_id=uuid4(),kind='help',title_key='help'))
    refs={'callback_answer_ref':uuid4()} if intent_kind=='callback_answer' else {'view_ref':payload.view_id}
    intent=DeliveryIntent(outbox_id=outbox_id,owner_id=owner,delivery_target_id=target,kind=intent_kind,
        dedupe_key=str(uuid4()),created_at=now,**refs)
    work=WorkRef(job_id=uuid4(),kind='deliver_outbox',payload_ref=outbox_id,owner_id=owner,dedupe_key=str(uuid4()))
    with db.uow() as u:
        u.outbox.append_unique(OutboxRecord(outbox_id=outbox_id,owner_id=owner,intent=intent,payload=payload))
        u.work.enqueue_unique(work)
        u.commit()
    now=datetime.now(timezone.utc)
    with db.uow() as u:
        claim=u.work.claim_next('deliver_outbox','sender',now,1)
        permit=SendPermit(outbox_id=outbox_id,send_attempt_id=uuid4(),job_id=claim.job_id,fence_token=claim.fence_token,
            owner_id=owner,delivery_target_id=target,approved_at=now,payload_hash=content_hash(payload),expires_at=now+timedelta(seconds=1))
        assert u.outbox.begin_send_if_allowed(claim,permit,now).ok
        u.commit()
    receipt=(MessageReceipt(operation='edit_message' if invalid_receipt=='edit' else 'send_message',message_id='verified-message',accepted_at=now)
        if invalid_receipt in ('message','edit') or (invalid_receipt=='valid' and intent_kind=='view')
        else CallbackReceipt(callback_id='foreign-callback' if invalid_receipt=='foreign_callback' else 'verified-callback',acknowledged_at=now))
    with db.uow() as u:
        recorded=u.outbox.record_transport_result(claim,TransportResult(status='confirmed',receipt=receipt),now)
        if invalid_receipt=='valid':
            assert recorded
            assert u.outbox.get(outbox_id).status=='confirmed'
            assert u.work.finish_if_claim(claim,now)
            u.commit()
            return
        assert not recorded
        assert u.outbox.get(outbox_id).status=='sending'
        assert u.work.get(work.job_id).status=='running'
        u.commit()
    with db.uow() as u:
        report=u.work.recover_expired(now+timedelta(seconds=2))
        assert outbox_id in report.unknown_delivery_ids
        assert u.outbox.get(outbox_id).status=='delivery_unknown'
        assert u.work.get(work.job_id).status=='failed'
        u.commit()


def test_crash_recovery_is_bounded_scoped_and_rejects_old_fences(db):
    from tsr.contracts import WorkRef,NormalizedEvent,StartEvent,InboxRecord
    now=datetime.now(timezone.utc)
    jobs={}
    with db.uow() as u:
        for scope in ('local-demo','production'):
            owner,target,inbox_id=uuid4(),uuid4(),uuid4()
            event=NormalizedEvent(inbox_id=inbox_id,bot_scope=scope,dedupe_key=str(uuid4()),owner_id=owner,
                delivery_target_id=target,kind='start',occurred_at=now,received_at=now,payload=StartEvent())
            u.inbox.insert(InboxRecord(inbox_id=inbox_id,owner_id=owner,bot_scope=scope,event_key=event.dedupe_key,event=event,received_at=now))
            work=WorkRef(job_id=uuid4(),kind='process_inbox',payload_ref=inbox_id,owner_id=owner,dedupe_key=str(uuid4()))
            u.work.enqueue_unique(work)
            jobs[scope]=work.job_id
        u.commit()
    now=datetime.now(timezone.utc)
    with db.uow() as u:
        foreign=u.work.claim_next('process_inbox','production',now,1,bot_scope='production')
        u.commit()
    old=None
    for attempt in range(1,6):
        with db.uow() as u:
            claim=u.work.claim_next('process_inbox','crashing-demo',now,1,bot_scope='local-demo')
            assert claim.job_id==jobs['local-demo']
            assert claim.attempt==attempt
            if old is not None:
                assert claim.fence_token>old.fence_token
                assert not u.work.finish_if_claim(old,now)
                assert not u.work.renew_lease(old,now,1)
            u.commit()
        now+=timedelta(seconds=2)
        with db.uow() as u:
            report=u.work.recover_expired(now,max_attempts=5,bot_scope='local-demo')
            assert u.work.get(foreign.job_id).status=='running'
            if attempt<5:
                assert jobs['local-demo'] in report.requeued_ids
                assert not report.exhausted_ids
            else:
                assert jobs['local-demo'] in report.exhausted_ids
                assert jobs['local-demo'] not in report.requeued_ids
                assert u.work.get(jobs['local-demo']).status=='failed'
                assert u.work.claim_next('process_inbox','sixth-attempt',now,1,bot_scope='local-demo') is None
            u.commit()
        old=claim


def test_recovery_marks_abandoned_failed_sending_unknown_without_reviving_jobs(db):
    from tsr.contracts import (WorkRef,OutboxRecord,DeliveryIntent,SendPermit,TransportResult,
        CallbackReceipt,ViewModel,IdentityRecord,content_hash)
    now=datetime.now(timezone.utc)

    def begin_send(scope):
        owner,target,outbox_id=uuid4(),uuid4(),uuid4()
        payload=ViewModel(view_id=uuid4(),kind='help',title_key='help')
        intent=DeliveryIntent(outbox_id=outbox_id,owner_id=owner,delivery_target_id=target,kind='view',
            view_ref=payload.view_id,dedupe_key=str(uuid4()),created_at=now)
        work=WorkRef(job_id=uuid4(),kind='deliver_outbox',payload_ref=outbox_id,owner_id=owner,dedupe_key=str(uuid4()))
        with db.uow() as u:
            u.identities.insert(IdentityRecord(identity_id=uuid4(),owner_id=owner,delivery_target_id=target,
                bot_scope=scope,lookup_key=str(uuid4())))
            u.outbox.append_unique(OutboxRecord(outbox_id=outbox_id,owner_id=owner,intent=intent,payload=payload))
            u.work.enqueue_unique(work)
            u.commit()
        claimed_at=datetime.now(timezone.utc)
        with db.uow() as u:
            claim=u.work.claim_next('deliver_outbox','sender',claimed_at,30,bot_scope=scope)
            permit=SendPermit(outbox_id=outbox_id,send_attempt_id=uuid4(),job_id=claim.job_id,fence_token=claim.fence_token,
                owner_id=owner,delivery_target_id=target,approved_at=claimed_at,payload_hash=content_hash(payload),expires_at=claimed_at+timedelta(seconds=30))
            assert u.outbox.begin_send_if_allowed(claim,permit,claimed_at).ok
            u.commit()
        return claim,outbox_id

    abandoned,abandoned_outbox=begin_send('demo')
    foreign,foreign_outbox=begin_send('production')
    current,current_outbox=begin_send('demo')
    observed=datetime.now(timezone.utc)
    for claim in (abandoned,foreign):
        with db.uow() as u:
            invalid=TransportResult(status='confirmed',receipt=CallbackReceipt(callback_id='unrelated',acknowledged_at=observed))
            assert not u.outbox.record_transport_result(claim,invalid,observed)
            assert u.work.finish_if_claim(claim,observed,status='failed')
            u.commit()
    with db.uow() as u:
        report=u.work.recover_expired(observed,bot_scope='demo')
        assert abandoned_outbox in report.unknown_delivery_ids
        assert foreign_outbox not in report.unknown_delivery_ids
        assert current_outbox not in report.unknown_delivery_ids
        assert not report.requeued_ids and not report.exhausted_ids
        assert u.outbox.get(abandoned_outbox).status=='delivery_unknown'
        assert u.work.get(abandoned.job_id).status=='failed'
        assert u.outbox.get(foreign_outbox).status=='sending'
        assert u.outbox.get(current_outbox).status=='sending'
        assert u.work.get(current.job_id).status=='running'
        assert u.work.claim_next('deliver_outbox','replacement',observed,30,bot_scope='demo') is None
        u.commit()
