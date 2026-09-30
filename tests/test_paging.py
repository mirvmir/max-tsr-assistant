"""User history pages must bound decryption, while preserving access to every row."""
from datetime import datetime,timedelta,timezone
import pytest
from test_db import db
from test_maintenance import identity,case


def test_case_pages_bound_decoding_preserve_history_and_exclude_foreign_deleted(db,monkeypatch):
    now=datetime.now(timezone.utc)
    with db.uow() as unit:
        owner=identity(unit,'paging-owner')
        foreign=identity(unit,'paging-foreign')
        records=[case(unit,owner,now+timedelta(seconds=i)) for i in range(31)]
        deleted=case(unit,owner,now+timedelta(days=1))
        unit.cases.save(deleted.model_copy(update={'status':'deleted'}))
        case(unit,foreign,now+timedelta(days=2))
        unit.commit()
    with db.uow() as unit:
        calls=[]
        original=unit.cases._decode
        def observed(row):
            calls.append(row['id'] if row else None)
            return original(row)
        monkeypatch.setattr(unit.cases,'_decode',observed)
        assert unit.cases.count_live_by_owner(owner.owner_id)==31
        assert calls==[]
        page=unit.cases.page_live_by_owner(owner.owner_id,5,0)
        assert [r.case_id for r in page]==[r.case_id for r in records[-5:][::-1]]
        assert len(calls)==5
        assert unit.cases.latest_live_by_owner(owner.owner_id).case_id==records[-1].case_id
        assert len(calls)==6
        all_ids={r.case_id for offset in range(0,31,5) for r in unit.cases.page_live_by_owner(owner.owner_id,5,offset)}
        assert all_ids=={r.case_id for r in records}
        assert not unit.cases.page_live_by_owner(owner.owner_id,5,31)
        with pytest.raises(ValueError):unit.cases.page_live_by_owner(owner.owner_id,101,0)


def view_record(ctx,snapshot,now,dialog=0,status='pending',receipt=None):
    from uuid import uuid4
    from tsr import contracts as c
    view=c.ViewModel(view_id=uuid4(),kind='input',title_key='input',case_guard=snapshot.guard,dialog_revision=dialog)
    id=uuid4()
    intent=c.DeliveryIntent(outbox_id=id,owner_id=ctx.owner_id,delivery_target_id=ctx.delivery_target_id,
        case_guard=snapshot.guard,kind='view',view_ref=view.view_id,dedupe_key=uuid4().hex,created_at=now)
    return c.OutboxRecord(outbox_id=id,owner_id=ctx.owner_id,intent=intent,payload=view,status=status,
        transport_result=c.TransportResult(status='confirmed',receipt=receipt) if receipt else None)


def test_current_dialog_guard_and_handle_lookups_decode_only_exact_rows(db,monkeypatch):
    from uuid import uuid4
    from tsr import contracts as c
    now=datetime.now(timezone.utc)
    with db.uow() as u:
        ctx=identity(u,'bounded-dialog');other=identity(u,'foreign');snapshot=case(u,ctx,now)
        for i in range(35):
            u.outbox.append_unique(view_record(ctx,snapshot,now+timedelta(seconds=i),dialog=i))
            candidate=c.Candidate(candidate_id=uuid4(),owner_id=ctx.owner_id,case_id=snapshot.case_id,
                field_key='name',value=c.TextValue(value='sensitive candidate'),value_hash='a'*64,
                case_guard=snapshot.guard,dialog_revision=i,created_at=now+timedelta(seconds=i),expires_at=now+timedelta(hours=1))
            u.candidates.insert(candidate)
        expected=candidate
        expired=candidate.model_copy(update={'candidate_id':uuid4(),'created_at':now+timedelta(days=1),'expires_at':now})
        stale=candidate.model_copy(update={'candidate_id':uuid4(),'created_at':now+timedelta(days=2),'case_guard':snapshot.guard.model_copy(update={'expected_revision':1})})
        u.candidates.insert(expired);u.candidates.insert(stale)
        handle=c.HandleRecord(handle_id=uuid4(),handle=uuid4().hex,owner_id=ctx.owner_id,case_guard=snapshot.guard,
            dialog_revision=34,expires_at=now-timedelta(seconds=1),action=c.ActionIntent(action_key='help',label_key='help',
            command_type='navigate',typed_payload=c.NavigatePayload(destination='help')))
        u.handles.insert(handle);u.commit()
    with db.uow() as u:
        calls=[]
        for repo in (u.outbox,u.candidates,u.handles):
            original=repo._decode
            def observed(row,original=original):
                if row:calls.append(row['id'])
                return original(row)
            monkeypatch.setattr(repo,'_decode',observed)
        assert u.outbox.latest_question(ctx.owner_id,snapshot.case_id,34).payload.dialog_revision==34
        assert u.outbox.latest_question(other.owner_id,snapshot.case_id,34) is None
        assert u.outbox.latest_for_owner_case(ctx.owner_id,snapshot.case_id).payload.dialog_revision==34
        assert u.candidates.latest_for_guard(ctx.owner_id,snapshot.guard,now)==expected
        assert u.candidates.latest_for_guard(ctx.owner_id,snapshot.guard,now,dialog_revision=34)==expected
        assert u.candidates.latest_for_guard(ctx.owner_id,snapshot.guard,now,dialog_revision=99) is None
        assert u.candidates.latest_for_guard(other.owner_id,snapshot.guard,now) is None
        assert u.handles.find(ctx.owner_id,snapshot.case_id,handle.handle)==handle
        assert u.handles.find(other.owner_id,snapshot.case_id,handle.handle) is None
        assert len(calls)==5
        assert u.connection.execute('SELECT active FROM callback_handles WHERE id=%s',(handle.handle_id,)).fetchone()['active']


def test_receipt_lookup_is_typed_owner_target_bound_private_and_backfilled(db,monkeypatch):
    from uuid import uuid4
    from tsr import contracts as c
    now=datetime.now(timezone.utc)
    with db.uow() as u:
        ctx=identity(u,'receipt-owner');snapshot=case(u,ctx,now)
        for i in range(35):
            receipt=c.MessageReceipt(operation='send_message',message_id='private-message-'+str(i),accepted_at=now)
            record=view_record(ctx,snapshot,now+timedelta(seconds=i),status='confirmed',receipt=receipt)
            u.outbox.append_unique(record)
        expected=receipt
        u.commit()
    with db.uow() as u:
        # Simulate legacy rows; startup backfill must leave immutable ciphertext unchanged.
        old=u.connection.execute('SELECT ciphertext FROM outbox_messages WHERE id=%s',(record.outbox_id,)).fetchone()['ciphertext']
        u.connection.execute('UPDATE outbox_messages SET receipt_digest=NULL,delivery_target_id=NULL,dialog_revision=NULL,payload_kind=NULL')
        u.connection.execute('UPDATE user_identities SET delivery_target_id=NULL');u.commit()
    db.migrate();db.migrate()
    with db.uow() as u:
        calls=[];original=u.outbox._decode
        def observed(row):
            if row:calls.append(row['id'])
            return original(row)
        monkeypatch.setattr(u.outbox,'_decode',observed)
        assert u.outbox.receipt_exists(ctx.owner_id,ctx.delivery_target_id,expected)
        assert not u.outbox.receipt_exists(uuid4(),ctx.delivery_target_id,expected)
        assert not u.outbox.receipt_exists(ctx.owner_id,uuid4(),expected)
        assert not u.outbox.receipt_exists(ctx.owner_id,ctx.delivery_target_id,expected.model_copy(update={'operation':'edit_message'}))
        assert not u.outbox.receipt_exists(ctx.owner_id,ctx.delivery_target_id,c.CallbackReceipt(callback_id=expected.message_id,acknowledged_at=now))
        assert calls==[record.outbox_id]
        row=u.connection.execute('SELECT * FROM outbox_messages WHERE id=%s',(record.outbox_id,)).fetchone()
        assert bytes(row['ciphertext'])==bytes(old)
        assert expected.message_id not in str({k:v for k,v in row.items() if k not in ('ciphertext','nonce','tag')})
        assert u.identities.find_by_target(ctx.owner_id,ctx.delivery_target_id).owner_id==ctx.owner_id
        assert u.identities.find_by_target(uuid4(),ctx.delivery_target_id) is None
        assert u.identities.find_by_target(ctx.owner_id,uuid4()) is None
        duplicate=c.IdentityRecord(identity_id=uuid4(),owner_id=ctx.owner_id,delivery_target_id=ctx.delivery_target_id,
            bot_scope=ctx.bot_scope,lookup_key=uuid4().hex)
        u.identities.insert(duplicate)
        assert u.identities.find_by_target(ctx.owner_id,ctx.delivery_target_id) is None
        assert len(u.identities.for_owner(ctx.owner_id,limit=1))==1
        with pytest.raises(ValueError):u.identities.for_owner(ctx.owner_id,limit=3)
        u.outbox.append_unique(view_record(ctx,snapshot,now,status='confirmed',receipt=expected))
        assert not u.outbox.receipt_exists(ctx.owner_id,ctx.delivery_target_id,expected)


def test_bulk_invalidation_preserves_inflight_and_denies_old_fences(db):
    from uuid import uuid4
    from tsr import contracts as c
    from test_maintenance import inbox
    now=datetime.now(timezone.utc)
    with db.uow() as u:
        ctx=identity(u,'bulk');foreign=identity(u,'bulk-other');snapshot=case(u,ctx,now)
        id=inbox(u,ctx,now,case_id=snapshot.case_id)
        safe=c.WorkRef(job_id=uuid4(),kind='process_inbox',payload_ref=id,owner_id=ctx.owner_id,
            case_id=snapshot.case_id,case_revision=0,deletion_epoch=0,dedupe_key=uuid4().hex)
        u.work.enqueue_unique(safe)
        sending=view_record(ctx,snapshot,now);u.outbox.append_unique(sending)
        send=c.WorkRef(job_id=uuid4(),kind='deliver_outbox',payload_ref=sending.outbox_id,owner_id=ctx.owner_id,
            case_id=snapshot.case_id,case_revision=0,deletion_epoch=0,dedupe_key=uuid4().hex)
        u.work.enqueue_unique(send)
        roles=[]
        for actor,guard,command in ((ctx,None,'start_case'),(ctx,snapshot.guard,'navigate'),(foreign,None,'start_case')):
            action=c.ActionIntent(action_key='role',label_key='role',command_type=command,
                typed_payload=c.StartCasePayload(category_ref=snapshot.profile_ref,requested_role='self') if command=='start_case' else c.NavigatePayload(destination='help'))
            handle=c.HandleRecord(handle_id=uuid4(),handle=uuid4().hex,owner_id=actor.owner_id,case_guard=guard,
                action=action,expires_at=now+timedelta(hours=1));u.handles.insert(handle);roles.append(handle)
        u.commit()
    now=datetime.now(timezone.utc)
    with db.uow() as u:
        old=u.work.claim_next('process_inbox','safe',now,60,bot_scope='bulk')
        sending_claim=u.work.claim_next('deliver_outbox','sender',now,60,bot_scope='bulk')
        permit=c.SendPermit(outbox_id=sending.outbox_id,send_attempt_id=uuid4(),job_id=send.job_id,
            fence_token=sending_claim.fence_token,owner_id=ctx.owner_id,delivery_target_id=ctx.delivery_target_id,
            case_guard=snapshot.guard,approved_at=now,payload_hash=c.content_hash(sending.payload),expires_at=now+timedelta(seconds=60))
        assert u.outbox.begin_send_if_allowed(sending_claim,permit,now).ok
        assert u.work.cancel_old_for_case(snapshot.case_id,foreign.owner_id,1)==0
        assert u.work.cancel_old_for_case(snapshot.case_id,ctx.owner_id,1)==1
        assert not u.work.finish_if_claim(old,now)
        assert u.work.get(safe.job_id).fence_token>old.fence_token
        assert u.work.is_claim_current(sending_claim,now)
        assert u.handles.invalidate_owner(ctx.owner_id)==1
        assert [u.connection.execute('SELECT active FROM callback_handles WHERE id=%s',(r.handle_id,)).fetchone()['active'] for r in roles]==[False,True,True]
        assert u.handles.invalidate_owner(ctx.owner_id,snapshot.case_id)==1


def test_frozen_lookup_and_bulk_bundle_status_preserve_ciphertext_and_scope(db,monkeypatch):
    from uuid import uuid4
    from tsr import contracts as c
    from test_documents import frozen_manifest
    now=datetime.now(timezone.utc);template=frozen_manifest()
    with db.uow() as u:
        ctx=identity(u,'frozen-pages');foreign=identity(u,'foreign-pages')
        inp=template.content.input_revision.model_copy(update={'owner_id':ctx.owner_id})
        snapshot=c.CaseSnapshot(case_id=inp.case_id,owner_id=ctx.owner_id,mode='demo',category_id=inp.category_id,
            profile_ref=inp.profile_ref,case_revision=2,input_revision_id=inp.input_revision_id,last_activity_at=now)
        u.inputs.insert(inp);u.cases.insert(snapshot)
        content=template.content.model_copy(update={'owner_id':ctx.owner_id,'input_revision':inp})
        expected_artifacts=[]
        for i in range(12):
            manifest=template.model_copy(update={'manifest_id':uuid4(),'confirmation_id':uuid4(),'content':content,'manifest_hash':c.content_hash(content)})
            u.confirmations.insert(c.Confirmation(confirmation_id=manifest.confirmation_id,owner_id=ctx.owner_id,
                case_id=snapshot.case_id,case_revision=2,deletion_epoch=0,preview_id=uuid4(),manifest_hash=manifest.manifest_hash,confirmed_at=now))
            u.manifests.insert(manifest)
            bundle=c.DocumentBundle(bundle_id=uuid4(),owner_id=ctx.owner_id,case_id=snapshot.case_id,manifest_id=manifest.manifest_id,
                required_artifacts=(),status='ready',created_at=now)
            u.bundles.insert(bundle)
            for kind in ('purchase_card','application','product_card','checklist'):
                artifact=c.ArtifactRecord(artifact_id=uuid4(),job_id=uuid4(),fence_token=1,manifest_id=manifest.manifest_id,
                    manifest_hash=manifest.manifest_hash,document_kind=kind,format='pdf',plaintext_sha256='a'*64,
                    encrypted_blob_ref=uuid4().hex,size_bytes=12,created_at=now,owner_id=ctx.owner_id,case_id=snapshot.case_id,
                    case_revision=2,deletion_epoch=0,published_at=now,expires_at=now+timedelta(days=1))
                u.artifacts.insert(artifact)
                if i==11:expected_artifacts.append(artifact)
        immutable_before={row['id']:bytes(row['ciphertext']) for row in u.connection.execute('SELECT id,ciphertext FROM document_bundles').fetchall()}
        u.commit()
    with db.uow() as u:
        decoded=[]
        for repo in (u.manifests,u.artifacts,u.bundles):
            original=repo._decode
            def observed(row,original=original):
                if row:decoded.append(row['id'])
                return original(row)
            monkeypatch.setattr(repo,'_decode',observed)
        assert u.manifests.find_by_confirmation(manifest.confirmation_id,ctx.owner_id,snapshot.case_id)==manifest
        assert u.manifests.find_by_confirmation(manifest.confirmation_id,foreign.owner_id,snapshot.case_id) is None
        ids=tuple(a.artifact_id for a in expected_artifacts)
        assert set(a.artifact_id for a in u.artifacts.get_many(ids,ctx.owner_id,snapshot.case_id))==set(ids)
        assert not u.artifacts.get_many(ids,foreign.owner_id,snapshot.case_id)
        with pytest.raises(ValueError):u.artifacts.get_many(ids+(uuid4(),),ctx.owner_id,snapshot.case_id)
        assert len(decoded)==5
        assert u.bundles.mark_historical(snapshot.case_id,foreign.owner_id)==0
        assert u.bundles.mark_historical(snapshot.case_id,ctx.owner_id)==12
        assert u.bundles.mark_historical(snapshot.case_id,ctx.owner_id)==0
        assert len(decoded)==5
        after={row['id']:bytes(row['ciphertext']) for row in u.connection.execute('SELECT id,ciphertext FROM document_bundles').fetchall()}
        assert after==immutable_before
        assert u.bundles.get(bundle.bundle_id).status=='historical'


def test_attachment_cache_exact_hash_latest_and_backfill_bound_one_decode(db,monkeypatch):
    from uuid import uuid4
    from tsr import contracts as c
    from test_documents import frozen_manifest
    now=datetime.now(timezone.utc);manifest=frozen_manifest();content=manifest.content
    with db.uow() as u:
        u.inputs.insert(content.input_revision)
        snapshot=c.CaseSnapshot(case_id=content.case_id,owner_id=content.owner_id,mode='demo',category_id=content.input_revision.category_id,
            profile_ref=content.category_profile_ref,case_revision=2,input_revision_id=content.input_revision.input_revision_id,last_activity_at=now)
        u.cases.insert(snapshot)
        u.confirmations.insert(c.Confirmation(confirmation_id=manifest.confirmation_id,owner_id=content.owner_id,case_id=content.case_id,
            case_revision=2,deletion_epoch=0,preview_id=uuid4(),manifest_hash=manifest.manifest_hash,confirmed_at=now))
        u.manifests.insert(manifest)
        artifact=c.ArtifactRecord(artifact_id=uuid4(),job_id=uuid4(),fence_token=1,manifest_id=manifest.manifest_id,
            manifest_hash=manifest.manifest_hash,document_kind='application',format='pdf',plaintext_sha256='a'*64,
            encrypted_blob_ref=uuid4().hex,size_bytes=12,created_at=now,owner_id=content.owner_id,case_id=content.case_id,
            case_revision=2,deletion_epoch=0,published_at=now,expires_at=now+timedelta(days=1))
        u.artifacts.insert(artifact)
        for i in range(30):
            record=c.AttachmentRecord(attachment_token_ref=uuid4(),artifact_id=artifact.artifact_id,owner_id=content.owner_id,
                manifest_hash=manifest.manifest_hash if i in (5,15) else 'b'*64,state='ready',
                observed_at=now+timedelta(seconds=i),token='sensitive-token-'+str(i))
            u.attachments.insert(record)
            if i==15:expected=record
        before={row['id']:bytes(row['ciphertext']) for row in u.connection.execute('SELECT id,ciphertext FROM uploaded_attachments').fetchall()}
        u.connection.execute('UPDATE uploaded_attachments SET manifest_hash=NULL,observed_at=NULL');u.commit()
    db.migrate();db.migrate()
    with db.uow() as u:
        calls=[];original=u.attachments._decode
        def observed(row):
            if row:calls.append(row['id'])
            return original(row)
        monkeypatch.setattr(u.attachments,'_decode',observed)
        assert u.attachments.find_for_artifact(content.owner_id,artifact.artifact_id,manifest.manifest_hash)==expected
        assert u.attachments.find_for_artifact(uuid4(),artifact.artifact_id,manifest.manifest_hash) is None
        assert u.attachments.find_for_artifact(content.owner_id,uuid4(),manifest.manifest_hash) is None
        assert u.attachments.find_for_artifact(content.owner_id,artifact.artifact_id,'c'*64) is None
        assert calls==[expected.attachment_token_ref]
        after={row['id']:bytes(row['ciphertext']) for row in u.connection.execute('SELECT id,ciphertext FROM uploaded_attachments').fetchall()}
        assert before==after
