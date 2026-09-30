"""Real PostgreSQL policy boundaries, IO retry and scoped operational metadata."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import os
from uuid import uuid4
import pytest
from test_db import db
from tsr.contracts import (ActorContext,CaseSnapshot,InputRevision,IdentityRecord,VersionRef,
    NormalizedEvent,StartEvent,InboxRecord,WorkRef,RetentionPolicy,HandleRecord,ActionIntent,NavigatePayload)


def identity(u,scope):
    ctx=ActorContext(owner_id=uuid4(),delivery_target_id=uuid4(),bot_scope=scope,case_mode='demo',correlation_id=uuid4())
    u.identities.insert(IdentityRecord(identity_id=uuid4(),owner_id=ctx.owner_id,delivery_target_id=ctx.delivery_target_id,
        bot_scope=scope,lookup_key=uuid4().hex))
    return ctx


def inbox(u,ctx,now,status='received',case_id=None):
    id=uuid4()
    event=NormalizedEvent(inbox_id=id,bot_scope=ctx.bot_scope,dedupe_key=uuid4().hex,owner_id=ctx.owner_id,
        delivery_target_id=ctx.delivery_target_id,kind='start',occurred_at=now,received_at=now,payload=StartEvent())
    u.inbox.insert(InboxRecord(inbox_id=id,owner_id=ctx.owner_id,bot_scope=ctx.bot_scope,event_key=event.dedupe_key,event=event,received_at=now))
    if status!='received':
        u.inbox.mark_processed(id,status,now=now)
    if case_id is not None:
        u.inbox.bind_case(id,ctx,case_id)
    return id


def case(u,ctx,last_activity):
    id,input_id=uuid4(),uuid4()
    ref=VersionRef(id='maintenance-test',version='1.0.0')
    revision=InputRevision(input_revision_id=input_id,owner_id=ctx.owner_id,case_id=id,created_at=last_activity,
        category_id='test',profile_ref=ref,role='self')
    snapshot=CaseSnapshot(case_id=id,owner_id=ctx.owner_id,mode='demo',category_id='test',profile_ref=ref,
        input_revision_id=input_id,last_activity_at=last_activity)
    u.inputs.insert(revision);u.cases.insert(snapshot)
    return snapshot


def test_health_persists_scoped_heartbeat_and_queue_ages(db):
    now=datetime.now(timezone.utc)
    with db.uow() as u:
        for scope in ('demo','foreign'):
            ctx=identity(u,scope);ref=inbox(u,ctx,now)
            u.work.enqueue_unique(WorkRef(job_id=uuid4(),kind='process_inbox',payload_ref=ref,owner_id=ctx.owner_id,dedupe_key=uuid4().hex))
        u.commit()
    db.record_worker_heartbeat('private-worker',now-timedelta(seconds=20),'test-commit',bot_scope='demo',capacity={'render_slots':1})
    health=db.collect_health(now,'demo')
    assert health.worker_count==1 and health.worker_heartbeat_age==20
    assert next(item for item in health.queue_delays if item.kind=='process_inbox').queued_count==1
    assert 'private-worker' not in health.model_dump_json()
    stale=db.collect_health(now+timedelta(seconds=41),'demo')
    assert stale.worker_count==0 and 'worker_missing' in stale.reasons


def test_retention_inbox_boundary_pending_scope_and_dedupe_are_safe(db,tmp_path):
    from tsr.adapters.files import PrivateFiles
    from tsr.operations.maintenance import run_retention
    now=datetime.now(timezone.utc);old=now-timedelta(hours=24)
    with db.uow() as u:
        own=identity(u,'demo');foreign=identity(u,'foreign')
        due=inbox(u,own,old,'processed');fresh=inbox(u,own,old+timedelta(microseconds=1),'ignored')
        pending=inbox(u,own,old-timedelta(days=10));other=inbox(u,foreign,old,'processed')
        handle=HandleRecord(handle_id=uuid4(),handle=uuid4().hex,owner_id=own.owner_id,expires_at=now,
            action=ActionIntent(action_key='help',label_key='help',command_type='navigate',typed_payload=NavigatePayload(destination='help')))
        u.handles.insert(handle);u.commit()
    files=PrivateFiles(tmp_path/'blobs',db.crypto)
    report=run_retention(now,RetentionPolicy(),db=db,files=files,bot_scope='demo')
    assert not report.errors
    with db.uow() as u:
        assert u.inbox.get(due) is None and u.handles.get(handle.handle_id) is None
        assert u.inbox.get(fresh) is not None and u.inbox.get(pending) is not None and u.inbox.get(other) is not None
        assert u.connection.execute('SELECT dedupe_key FROM inbox_events WHERE id=%s',(due,)).fetchone()['dedupe_key']
    again=run_retention(now,RetentionPolicy(),db=db,files=files,bot_scope='demo')
    assert not again.errors and again.removed_count==0


def test_case_retention_uses_epoch_journal_and_skips_foreign_active_claim(db,tmp_path):
    from tsr.adapters.files import PrivateFiles
    from tsr.operations.maintenance import run_retention
    now=datetime.now(timezone.utc);old=now-timedelta(days=90)
    with db.uow() as u:
        own=identity(u,'demo');foreign=identity(u,'foreign')
        due=case(u,own,old);other=case(u,foreign,old);active=case(u,own,old)
        ref=inbox(u,own,now,case_id=active.case_id)
        work=WorkRef(job_id=uuid4(),kind='process_inbox',payload_ref=ref,owner_id=own.owner_id,case_id=active.case_id,
            case_revision=0,deletion_epoch=0,dedupe_key=uuid4().hex)
        u.work.enqueue_unique(work);u.commit()
    with db.uow() as u:
        u.work.claim_next('process_inbox','live',now+timedelta(seconds=1),300,bot_scope='demo');u.commit()
    report=run_retention(now+timedelta(seconds=1),RetentionPolicy(),db=db,files=PrivateFiles(tmp_path/'blobs',db.crypto),bot_scope='demo')
    assert not report.errors
    with db.uow() as u:
        assert u.cases.get(due.case_id) is None
        assert u.cases.get(other.case_id).status=='active' and u.cases.get(active.case_id).status=='active'
    entry=next(item for item in db.export_deletion_journal() if item.case_id==due.case_id)
    assert entry.deletion_epoch==1 and entry.owner_id==own.owner_id


def test_artifact_retention_retries_failed_io_and_preserves_private_ref(db,tmp_path,monkeypatch):
    from tsr.application import Application
    from tsr.config import Settings
    from tsr.adapters.files import PrivateFiles
    from tsr.operations.releases import load_demo_release
    from tsr.operations.maintenance import run_retention
    from tsr.contracts import ArtifactRecord
    from test_application import prepare_flow
    root=Path(__file__).parents[1]
    app=Application(db,load_demo_release(root),Settings(database_url=os.environ['TSR_TEST_DATABASE_URL'],encryption_key='78'*32,identity_hmac_key='test'))
    with db.uow() as u:
        ctx=identity(u,'demo');u.commit()
    snapshot,manifest=prepare_flow(app,ctx,'purchase')
    now=datetime.now(timezone.utc);artifact_id=uuid4()
    files=PrivateFiles(tmp_path/'blobs',db.crypto)
    ref=artifact_id.hex+'.blob';path=files.root/ref
    path.write_bytes(db.crypto.encrypt(b'synthetic-test-bytes').ciphertext)
    record=ArtifactRecord(artifact_id=artifact_id,job_id=uuid4(),fence_token=1,manifest_id=manifest.manifest_id,
        manifest_hash=manifest.manifest_hash,document_kind='purchase_card',format='pdf',plaintext_sha256='a'*64,
        encrypted_blob_ref=ref,size_bytes=20,created_at=now-timedelta(days=7),owner_id=ctx.owner_id,case_id=snapshot.case_id,
        case_revision=snapshot.case_revision,deletion_epoch=0,published_at=now-timedelta(days=7),expires_at=now)
    with db.uow() as u:
        u.artifacts.insert(record);u.commit()
    original=Path.unlink
    def fail_once(target,*args,**kwargs):
        if target==path:
            raise OSError('synthetic IO failure')
        return original(target,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path,'unlink',fail_once)
        report=run_retention(now,RetentionPolicy(),db=db,files=files,bot_scope='demo')
    assert 'retention.file_unavailable' in report.errors and path.exists()
    with db.uow() as u:
        preserved=u.artifacts.get(artifact_id)
        assert preserved.publication_status=='expired' and preserved.encrypted_blob_ref==ref
    retried=run_retention(now,RetentionPolicy(),db=db,files=files,bot_scope='demo')
    assert not retried.errors and not path.exists()
    with db.uow() as u:
        assert u.artifacts.get(artifact_id) is None


def test_external_deletion_journal_survives_absent_case_and_replays_monotonic_epoch(db):
    from tsr.contracts import DeletionJournalEntry
    now=datetime.now(timezone.utc)
    with db.uow() as u:
        ctx=identity(u,'demo');snapshot=case(u,ctx,now);u.commit()
    absent=DeletionJournalEntry(case_id=uuid4(),owner_id=uuid4(),deletion_epoch=3,deleted_at=now,bot_scope='demo')
    entry=DeletionJournalEntry(case_id=snapshot.case_id,owner_id=ctx.owner_id,deletion_epoch=4,deleted_at=now,bot_scope='demo')
    assert db.apply_deletion_journal((entry,absent))==2
    assert db.apply_deletion_journal((entry,absent))==0
    with db.uow() as u:
        row=u.cases.get(snapshot.case_id)
        assert row.status=='deleted' and row.deletion_epoch==4
    assert len(db.export_deletion_journal('demo'))==2
    with pytest.raises(ValueError,match='ACCESS_DENIED'):
        db.apply_deletion_journal((entry.model_copy(update={'owner_id':uuid4()}),))


def test_release_pointer_and_leaf_revocation_are_exact_scope_and_ref(db):
    from tsr.operations.releases import load_demo_release
    from test_releases import _record
    from tsr.contracts import ReleaseLifecycle
    now=datetime.now(timezone.utc)
    release=load_demo_release(Path(__file__).parents[1]);first=_record(release,now)
    second_ref=VersionRef(id='isolated-synthetic-demo',version='1.0.0')
    second_manifest=first.manifest.model_copy(update={'release_id':second_ref.id})
    second=first.model_copy(update={'ref':second_ref,'manifest':second_manifest})
    with db.uow() as u:
        u.releases.insert(first);u.releases.insert(second)
        u.releases.activate(first.ref,'demo','operator',now,allow_synthetic_draft=True,bot_scope='live:public')
        u.releases.activate(second.ref,'demo','local',now,allow_synthetic_draft=True,bot_scope='local:synthetic')
        assert u.releases.get_active('demo',bot_scope='live:public').ref==first.ref
        assert u.releases.get_active('demo',bot_scope='local:synthetic').ref==second.ref
        assert u.releases.get_active('demo') is None
        u.releases.revoke_package(release.route.ref,'test.revoke','operator',now)
        assert not u.releases.read_lifecycle(first.ref).revoked
        with pytest.raises(ValueError,match='DATA_REVOKED'):
            u.releases.activate(first.ref,'demo','operator',now,allow_synthetic_draft=True,bot_scope='live:public')
        # The failed guarded activation has not altered either scoped pointer.
        assert u.releases.get_active('demo',bot_scope='live:public').ref==first.ref
        u.commit()


def test_database_query_and_lock_deadlines_are_bounded(db):
    import time
    import psycopg
    from tsr.adapters.db import Database
    limited=Database(db.dsn,db.crypto,statement_timeout_ms=100,lock_timeout_ms=100)
    started=time.monotonic()
    with pytest.raises(psycopg.errors.QueryCanceled):
        with limited.uow() as u:
            u.connection.execute('SELECT pg_sleep(1)')
    assert time.monotonic()-started<1.5
    now=datetime.now(timezone.utc)
    with db.uow() as u:
        ctx=identity(u,'demo');snapshot=case(u,ctx,now);u.commit()
    with db.uow() as holder:
        holder.cases.lock_owned(ctx,snapshot.case_id)
        started=time.monotonic()
        with pytest.raises((psycopg.errors.LockNotAvailable,psycopg.errors.QueryCanceled)):
            with limited.uow() as waiter:
                waiter.cases.lock_owned(ctx,snapshot.case_id)
        assert time.monotonic()-started<1.5


def test_retention_orphan_sweep_protects_live_render_claim_then_cleans_failed_stage(db,tmp_path):
    from hashlib import sha256
    from tsr.application import Application
    from tsr.config import Settings
    from tsr.adapters.files import PrivateFiles
    from tsr.operations.releases import load_demo_release
    from tsr.operations.maintenance import run_retention
    from tsr.contracts import RenderedBytes
    from test_application import prepare_flow
    app=Application(db,load_demo_release(Path(__file__).parents[1]),Settings(database_url=os.environ['TSR_TEST_DATABASE_URL'],encryption_key='78'*32,identity_hmac_key='test'))
    with db.uow() as u:
        ctx=identity(u,'demo');u.commit()
    _,manifest=prepare_flow(app,ctx,'purchase')
    now=datetime.now(timezone.utc)
    with db.uow() as u:
        claim=u.work.claim_next('render_artifact','live-render',now,120,bot_scope='demo');u.commit()
    context=app.get_render_context(claim,now)
    assert context.ok
    data=b'synthetic-render-fixture'
    rendered=RenderedBytes(job_id=claim.job_id,fence_token=claim.fence_token,manifest_hash=manifest.manifest_hash,
        format='pdf',mime_type='application/pdf',plaintext_sha256=sha256(data).hexdigest(),bytes=data)
    files=PrivateFiles(tmp_path/'blobs',db.crypto)
    staged=files.stage_encrypted(rendered,claim,context.value.payload)
    assert staged.ok
    path=files.root/staged.value.encrypted_blob_ref
    old=now-timedelta(minutes=10)
    os.utime(path,(old.timestamp(),old.timestamp()))
    report=run_retention(now,RetentionPolicy(),db=db,files=files,bot_scope='demo')
    assert not report.errors and path.exists()
    with db.uow() as u:
        assert u.work.finish_if_claim(claim,now,status='failed');u.commit()
    cleaned=run_retention(now,RetentionPolicy(),db=db,files=files,bot_scope='demo')
    assert not cleaned.errors and not path.exists()
