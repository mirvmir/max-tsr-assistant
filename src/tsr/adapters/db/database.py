from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generic, TypeVar
from uuid import UUID, uuid4
import psycopg
from psycopg.rows import dict_row
from psycopg import sql
from tsr import contracts as c
from tsr.ports import CryptoPort

T = TypeVar('T')


def success(value):
    return c.Result(ok=True,value=value)


def failure(code,ctx=None):
    return c.Result(ok=False,error=c.DomainError(code=code,field_key=None,retryability='none',
        safe_message_key='error.'+code.lower(),correlation_id=getattr(ctx,'correlation_id',None)))


def entity_id(dto):
    primary_fields={
        'CaseSnapshot':'case_id','InputRevision':'input_revision_id','Candidate':'candidate_id',
        'ResultPreview':'preview_id','Confirmation':'confirmation_id','DocumentManifest':'manifest_id',
        'DocumentBundle':'bundle_id','ArtifactRecord':'artifact_id','StagedArtifact':'artifact_id',
        'IdentityRecord':'identity_id','InboxRecord':'inbox_id','HandleRecord':'handle_id',
        'OutboxRecord':'outbox_id','ComparisonRecord':'comparison_id','AttachmentRecord':'attachment_token_ref',
    }
    field=primary_fields.get(type(dto).__name__)
    if field is None:
        raise TypeError('Unsupported record DTO')
    return getattr(dto,field)


def metadata(dto):
    content=getattr(dto,'content',None)
    guard=getattr(dto,'case_guard',None)
    intent=getattr(dto,'intent',None)
    if intent is not None:
        guard=intent.case_guard
    owner=getattr(dto,'owner_id',None) or getattr(content,'owner_id',None)
    case_id=getattr(dto,'case_id',None) or getattr(guard,'case_id',None) or getattr(content,'case_id',None)
    revision=getattr(dto,'case_revision',None)
    if revision is None:
        revision=getattr(guard,'expected_revision',None) if guard is not None else getattr(content,'case_revision',None)
    epoch=getattr(dto,'deletion_epoch',None)
    if epoch is None:
        epoch=getattr(guard,'expected_deletion_epoch',0) if guard is not None else getattr(content,'deletion_epoch',0)
    kind=getattr(dto,'document_kind',None)
    if kind is not None:
        kind=f'{kind}:{getattr(dto,"format","")}'
    return dict(id=entity_id(dto),owner_id=owner,case_id=case_id,revision=revision,deletion_epoch=epoch or 0,
        bot_scope=getattr(dto,'bot_scope',''),status=getattr(dto,'status',getattr(dto,'publication_status','active')),
        dedupe_key=getattr(dto,'event_key',None) or getattr(intent,'dedupe_key',None),
        parent_id=getattr(dto,'confirmation_id',None) if content is not None else None,
        manifest_id=getattr(dto,'manifest_id',None) if type(dto).__name__ != 'DocumentManifest' else entity_id(dto),
        preview_id=getattr(dto,'preview_id',None) if type(dto).__name__ != 'ResultPreview' else entity_id(dto),
        payload_ref=None,kind=kind,opaque_key=getattr(dto,'lookup_key',None) or getattr(dto,'handle',None),
        expires_at=getattr(dto,'expires_at',None),active=True)


class EncryptedRepository(Generic[T]):
    def __init__(self,uow,table,model,immutable=False):
        self.uow,self.connection,self.table,self.model,self.immutable=uow,uow.connection,table,model,immutable

    def _encrypted(self,dto):
        blob=self.uow.crypto.encrypt(dto.model_dump_json().encode('utf-8'))
        return dict(key_id=blob.key_id,nonce=blob.nonce,ciphertext=blob.ciphertext,tag=blob.tag)

    def _decode(self,row):
        if row is None or not row['ciphertext']:
            return None
        blob=c.EncryptedBlob(**{key:(bytes(row[key]) if key!='key_id' else row[key]) for key in ('key_id','nonce','ciphertext','tag')})
        dto=self.model.model_validate_json(self.uow.crypto.decrypt(blob))
        # Status is an operational column; never accept stale encrypted status after a CAS.
        status_field='publication_status' if 'publication_status' in self.model.model_fields else 'status'
        if status_field in self.model.model_fields and row.get('status') is not None:
            dto=dto.model_copy(update={status_field:row['status']})
        return dto

    def _one(self,where,args=(),lock=False):
        query=sql.SQL('SELECT * FROM {} WHERE '+where+(' FOR UPDATE' if lock else '')).format(sql.Identifier(self.table))
        return self.connection.execute(query,args).fetchone()

    def get(self,id):
        return self._decode(self._one('id=%s',(id,)))

    def get_owned(self,ctx,id,lock=False):
        row=self._one('id=%s AND owner_id=%s',(id,ctx.owner_id),lock)
        if row is None:
            return failure('ACCESS_DENIED',ctx)
        case_id=row.get('case_id')
        if case_id is not None and self.table != 'cases':
            case=self.connection.execute('SELECT status FROM cases WHERE id=%s AND owner_id=%s',(case_id,ctx.owner_id)).fetchone()
            if case is None or case['status']=='deleted':
                return failure('CASE_DELETED',ctx)
        if row['status']=='deleted':
            return failure('CASE_DELETED',ctx)
        return success(self._decode(row))

    def _write(self,dto,update=False,conflict=False):
        values=metadata(dto)|self._encrypted(dto)
        if self.table=='uploaded_attachments':
            artifact=self.connection.execute('SELECT owner_id,case_id,manifest_id FROM document_artifacts WHERE id=%s',(dto.artifact_id,)).fetchone()
            if artifact is None or artifact['owner_id']!=dto.owner_id:
                raise PermissionError('Artifact unavailable')
            values.update(artifact_id=dto.artifact_id,case_id=artifact['case_id'],manifest_id=artifact['manifest_id'])
        if values['case_id'] is not None and self.table!='cases':
            row=self.connection.execute('SELECT status FROM cases WHERE id=%s AND owner_id=%s',(values['case_id'],values['owner_id'])).fetchone()
            terminal_send=update and self.table=='outbox_messages' and values['status'] in ('confirmed','definitely_rejected','delivery_unknown','retry_wait')
            if row is not None and row['status']=='deleted' and not terminal_send:
                raise PermissionError('Case unavailable')
        columns=list(values)
        if update:
            if self.immutable:
                raise ValueError('Immutable record cannot be overwritten')
            assignments=sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(k)) for k in columns if k!='id')
            query=sql.SQL('UPDATE {} SET {} WHERE id=%s AND owner_id=%s').format(sql.Identifier(self.table),assignments)
            result=self.connection.execute(query,tuple(values[k] for k in columns if k!='id')+(values['id'],values['owner_id']))
            if result.rowcount!=1:
                raise LookupError('Record unavailable')
        else:
            query=sql.SQL('INSERT INTO {} ({}) VALUES ({})'+(' ON CONFLICT DO NOTHING' if conflict else '')).format(
                sql.Identifier(self.table),sql.SQL(',').join(map(sql.Identifier,columns)),sql.SQL(',').join(sql.Placeholder() for _ in columns))
            self.connection.execute(query,tuple(values.values()))
        return dto

    def insert(self,dto):
        return self._write(dto)

    def save(self,dto):
        return self._write(dto,update=True)

    def _list(self,field,value):
        rows=self.connection.execute(sql.SQL('SELECT * FROM {} WHERE {}=%s ORDER BY id').format(sql.Identifier(self.table),sql.Identifier(field)),(value,)).fetchall()
        return tuple(dto for row in rows if (dto:=self._decode(row)) is not None)

    def list_by_case(self,case_id):
        return self._list('case_id',case_id)

    def list_by_owner(self,owner_id):
        return self._list('owner_id',owner_id)

    def find_by_manifest(self,manifest_id):
        return self._decode(self._one('manifest_id=%s',(manifest_id,)))

    def find_by_preview(self,preview_id):
        return self._decode(self._one('preview_id=%s',(preview_id,)))


class CaseRepository(EncryptedRepository):
    def lock_owned(self,ctx,id):
        return self.get_owned(ctx,id,True)

    def check_guard(self,ctx,guard):
        result=self.lock_owned(ctx,guard.case_id)
        if not result.ok:
            return result
        case=result.value
        if case.deletion_epoch!=guard.expected_deletion_epoch or case.case_revision!=guard.expected_revision:
            return failure('STALE_REVISION',ctx)
        return result

    def save_if_revision(self,dto,guard):
        row=self._one('id=%s AND owner_id=%s',(guard.case_id,dto.owner_id),True)
        if row is None:
            return failure('ACCESS_DENIED')
        if row['status']=='deleted':
            return failure('CASE_DELETED')
        if row['revision']!=guard.expected_revision or row['deletion_epoch']!=guard.expected_deletion_epoch:
            return failure('STALE_REVISION')
        return success(self.save(dto))

    def mark_deleted(self,ctx,guard,now):
        result=self.check_guard(ctx,guard)
        if not result.ok:
            return result
        epoch=guard.expected_deletion_epoch+1
        case=result.value.model_copy(update={'status':'deleted','deletion_epoch':epoch})
        self.save(case)
        self.connection.execute('INSERT INTO case_tombstones(case_id,owner_id,deletion_epoch,deleted_at) VALUES(%s,%s,%s,%s) ON CONFLICT(case_id) DO NOTHING',(guard.case_id,ctx.owner_id,epoch,now))
        self.connection.execute('INSERT INTO cleanup_requests(case_id,deletion_epoch) VALUES(%s,%s) ON CONFLICT(case_id) DO NOTHING',(guard.case_id,epoch))
        self.connection.execute('UPDATE callback_handles SET active=false WHERE case_id=%s',(guard.case_id,))
        self.connection.execute("UPDATE jobs SET status='cancelled',lease_until=NULL WHERE case_id=%s AND status IN ('queued','retry_wait','running') AND NOT(kind='deliver_outbox' AND outbox_id IN (SELECT id FROM outbox_messages WHERE status='sending'))",(guard.case_id,))
        self.connection.execute("UPDATE document_bundles SET status='deleted' WHERE case_id=%s",(guard.case_id,))
        self.connection.execute("UPDATE document_artifacts SET status='deleted' WHERE case_id=%s",(guard.case_id,))
        sending=self.connection.execute("SELECT 1 FROM outbox_messages WHERE case_id=%s AND status='sending' LIMIT 1",(guard.case_id,)).fetchone()
        return success(c.DeleteReceipt(case_id=guard.case_id,deletion_epoch=epoch,own_access_revoked_at=now,cleanup='pending',inflight_delivery_possible=sending is not None))


class IdentityRepository(EncryptedRepository):
    def find(self,bot_scope,lookup_key):
        return self._decode(self._one('bot_scope=%s AND opaque_key=%s',(bot_scope,lookup_key)))

    def find_target(self,owner_id,target_id):
        return next((item for item in self.list_by_owner(owner_id) if item.delivery_target_id==target_id),None)

    def upsert(self,dto):
        self._write(dto,conflict=True)
        return self.find(dto.bot_scope,dto.lookup_key)


class InboxRepository(EncryptedRepository):
    def insert_unique(self,dto):
        existing=self._one('bot_scope=%s AND owner_id=%s AND dedupe_key=%s',(dto.bot_scope,dto.owner_id,dto.event_key))
        if existing:
            return self._decode(existing),False
        self._write(dto,conflict=True)
        persisted=self._one('bot_scope=%s AND owner_id=%s AND dedupe_key=%s',(dto.bot_scope,dto.owner_id,dto.event_key))
        return self._decode(persisted),persisted['id']==dto.inbox_id

    def bind_case(self,id,ctx,case_id):
        case=self.connection.execute('SELECT 1 FROM cases WHERE id=%s AND owner_id=%s',(case_id,ctx.owner_id)).fetchone()
        if case is None:
            raise PermissionError('Case unavailable')
        self.connection.execute('UPDATE inbox_events SET case_id=%s WHERE id=%s AND owner_id=%s',(case_id,id,ctx.owner_id))
    def mark_processed(self,id,status='processed'):
        if status not in ('processed','ignored','failed','received','processing'):
            raise ValueError('Invalid inbox status')
        self.connection.execute('UPDATE inbox_events SET status=%s WHERE id=%s',(status,id))


class HandleRepository(EncryptedRepository):
    def resolve(self,ctx,opaque_handle,now,consume=False):
        row=self._one('opaque_key=%s AND owner_id=%s',(opaque_handle,ctx.owner_id),True)
        if row is None:
            return failure('ACCESS_DENIED',ctx)
        if not row['active'] or row['expires_at']<=now:
            return failure('STALE_REVISION',ctx)
        handle=self._decode(row)
        if handle.case_guard is not None:
            result=self.uow.cases.check_guard(ctx,handle.case_guard)
            if not result.ok:
                return result
            if result.value.dialog_revision != handle.dialog_revision:
                return failure('STALE_REVISION',ctx)
        if consume:
            self.connection.execute('UPDATE callback_handles SET active=false WHERE id=%s',(row['id'],))
        return success(handle)


class AttachmentRepository(EncryptedRepository):
    def find_for_artifact(self,owner_id,artifact_id,manifest_hash):
        rows=self.connection.execute('SELECT * FROM uploaded_attachments WHERE owner_id=%s AND artifact_id=%s',(owner_id,artifact_id)).fetchall()
        records=(self._decode(row) for row in rows)
        matching=[record for record in records if record.manifest_hash==manifest_hash]
        return max(matching,key=lambda record:record.observed_at) if matching else None


class InputRepository(EncryptedRepository):
    append_revision=EncryptedRepository.insert
    get_revision=EncryptedRepository.get
    def put_candidate(self,dto):
        return self.uow.candidates.insert(dto)
    def consume_candidate(self,ctx,id,now):
        result=self.uow.candidates.get_owned(ctx,id,True)
        if not result.ok:
            return result
        row=self.uow.candidates._one('id=%s',(id,))
        if not row['active'] or (row['expires_at'] is not None and row['expires_at']<=now):
            return failure('STALE_CANDIDATE',ctx)
        self.connection.execute('UPDATE input_candidates SET active=false WHERE id=%s',(id,))
        return result

class WorkRepository:
    def __init__(self,uow):
        self.uow,self.connection=uow,uow.connection
    def _decode(self,row):
        if row is None or not row['ciphertext']:
            return None
        blob=c.EncryptedBlob(**{k:bytes(row[k]) if k!='key_id' else row[k] for k in ('key_id','nonce','ciphertext','tag')})
        record=c.JobRecord.model_validate_json(self.uow.crypto.decrypt(blob))
        return record.model_copy(update={k:row[k] for k in ('status','fence_token','lease_owner','lease_until','attempt','next_attempt_at','trace_id')})
    def get(self,id):
        return self._decode(self.connection.execute('SELECT * FROM jobs WHERE id=%s',(id,)).fetchone())
    def list_by_case(self,case_id):
        return tuple(self._decode(row) for row in self.connection.execute('SELECT * FROM jobs WHERE case_id=%s ORDER BY id',(case_id,)).fetchall())
    def list_by_owner(self,owner_id):
        return tuple(self._decode(row) for row in self.connection.execute('SELECT * FROM jobs WHERE owner_id=%s ORDER BY id',(owner_id,)).fetchall())
    def save(self,record):
        if record.status!='cancelled':
            raise ValueError('Work transitions require fenced methods')
        self.connection.execute("UPDATE jobs SET status='cancelled',fence_token=fence_token+1,lease_until=NULL,lease_owner=NULL WHERE id=%s AND owner_id=%s AND fence_token=%s",(record.work.job_id,record.work.owner_id,record.fence_token))
        return self.get(record.work.job_id)
    def enqueue_unique(self,work,render_payload=None):
        if work.case_id is not None:
            case=self.connection.execute('SELECT status,deletion_epoch FROM cases WHERE id=%s AND owner_id=%s',(work.case_id,work.owner_id)).fetchone()
            if case is None or case['status']=='deleted' or case['deletion_epoch']!=work.deletion_epoch:
                raise PermissionError('Case unavailable')
        now=datetime.now(timezone.utc)
        # Scope originates in trusted identity; no sensitive data in job columns.
        identity=self.connection.execute('SELECT bot_scope FROM user_identities WHERE owner_id=%s LIMIT 1',(work.owner_id,)).fetchone()
        scope=identity['bot_scope'] if identity else 'default'
        if work.kind=='process_inbox':
            inbox=self.connection.execute('SELECT owner_id,bot_scope FROM inbox_events WHERE id=%s',(work.payload_ref,)).fetchone()
            if inbox is None or inbox['owner_id']!=work.owner_id:
                raise PermissionError('Inbox scope mismatch')
            scope=inbox['bot_scope']
        elif work.kind=='render_artifact':
            manifest=self.connection.execute('SELECT owner_id,case_id FROM document_manifests WHERE id=%s',(work.payload_ref,)).fetchone()
            if manifest is None or manifest['owner_id']!=work.owner_id or manifest['case_id']!=work.case_id:
                raise PermissionError('Manifest scope mismatch')
        elif work.kind=='deliver_outbox':
            outbox=self.connection.execute('SELECT owner_id,case_id FROM outbox_messages WHERE id=%s',(work.payload_ref,)).fetchone()
            if outbox is None or outbox['owner_id']!=work.owner_id or outbox['case_id']!=work.case_id:
                raise PermissionError('Outbox scope mismatch')
        record=c.JobRecord(work=work,status='queued',fence_token=0,lease_owner=None,lease_until=None,attempt=0,
                           next_attempt_at=now,trace_id=uuid4(),bot_scope=scope,render_payload=render_payload)
        blob=self.uow.crypto.encrypt(record.model_dump_json().encode())
        fk={'process_inbox':'inbox_id','render_artifact':'manifest_id','deliver_outbox':'outbox_id'}[work.kind]
        query=sql.SQL('INSERT INTO jobs(id,owner_id,case_id,revision,deletion_epoch,bot_scope,kind,payload_ref,dedupe_key,{},next_attempt_at,trace_id,key_id,nonce,ciphertext,tag) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING').format(sql.Identifier(fk))
        self.connection.execute(query,(work.job_id,work.owner_id,work.case_id,work.case_revision,work.deletion_epoch,scope,work.kind,work.payload_ref,work.dedupe_key,work.payload_ref,now,record.trace_id,blob.key_id,blob.nonce,blob.ciphertext,blob.tag))
        row=self.connection.execute('SELECT * FROM jobs WHERE bot_scope=%s AND owner_id=%s AND case_id IS NOT DISTINCT FROM %s AND kind=%s AND dedupe_key=%s',(scope,work.owner_id,work.case_id,work.kind,work.dedupe_key)).fetchone()
        return self._decode(row).work
    def claim_next(self,kind,worker_id,now,lease_seconds,bot_scope=None):
        if lease_seconds<=0:
            raise ValueError('Lease must be positive')
        row=self.connection.execute("""WITH candidate AS (
          SELECT id FROM jobs WHERE kind=%s AND status IN ('queued','retry_wait') AND next_attempt_at<=%s AND (%s::text IS NULL OR bot_scope=%s)
          ORDER BY next_attempt_at,id FOR UPDATE SKIP LOCKED LIMIT 1)
          UPDATE jobs SET status='running',fence_token=fence_token+1,lease_owner=%s,lease_until=%s,attempt=attempt+1
          WHERE id=(SELECT id FROM candidate) RETURNING *""",(kind,now,bot_scope,bot_scope,worker_id,now+timedelta(seconds=lease_seconds))).fetchone()
        if row is None:
            return None
        record=self._decode(row)
        return c.ClaimedJob(**record.work.model_dump(),fence_token=record.fence_token,lease_owner=record.lease_owner,
            lease_until=record.lease_until,attempt=record.attempt,next_attempt_at=record.next_attempt_at,trace_id=record.trace_id)
    def is_claim_current(self,claim,now):
        return self.connection.execute("SELECT 1 FROM jobs WHERE id=%s AND status='running' AND fence_token=%s AND lease_owner=%s AND lease_until>%s FOR UPDATE",(claim.job_id,claim.fence_token,claim.lease_owner,now)).fetchone() is not None
    check_claim=is_claim_current
    def _cas(self,claim,now,assignment,values):
        query="UPDATE jobs SET "+assignment+" WHERE id=%s AND status='running' AND fence_token=%s AND lease_owner=%s AND lease_until>%s"
        return self.connection.execute(query,tuple(values)+(claim.job_id,claim.fence_token,claim.lease_owner,now)).rowcount==1
    def renew_lease(self,claim,now,lease_seconds):
        if lease_seconds<=0:
            raise ValueError('Lease must be positive')
        return self._cas(claim,now,'lease_until=%s',(now+timedelta(seconds=lease_seconds),))
    def finish_if_claim(self,claim,now,status='succeeded'):
        if status not in ('succeeded','failed','cancelled'):
            raise ValueError('Invalid final status')
        return self._cas(claim,now,'status=%s,lease_until=NULL',(status,))
    def retry_safe(self,claim,now,next_attempt_at):
        return self._cas(claim,now,"status='retry_wait',next_attempt_at=%s,lease_until=NULL",(next_attempt_at,))
    def recover_expired(self,now,max_attempts=5,bot_scope=None):
        if max_attempts<1:
            raise ValueError('Max attempts must be positive')
        # Hold job locks before outbox locks for both expired claims and
        # abandoned sends whose jobs already reached a non-running state.
        rows=self.connection.execute("""SELECT j.id,j.kind,j.outbox_id,j.attempt,j.fence_token,j.status FROM jobs j
          WHERE ((j.status='running' AND j.lease_until<=%s)
            OR (j.kind='deliver_outbox' AND j.status<>'running' AND EXISTS
                (SELECT 1 FROM outbox_messages o WHERE o.id=j.outbox_id AND o.status='sending')))
          AND (%s::text IS NULL OR j.bot_scope=%s)
          ORDER BY j.lease_until NULLS FIRST,j.id FOR UPDATE OF j SKIP LOCKED""",(now,bot_scope,bot_scope)).fetchall()
        unknown=self.uow.outbox._recover_sending_for_jobs(tuple(row['id'] for row in rows)).unknown_delivery_ids
        requeued,exhausted=[],[]
        for row in rows:
            if row['status']!='running':
                continue
            delivery_unknown=False
            if row['kind']=='deliver_outbox':
                outbox=self.connection.execute('SELECT status FROM outbox_messages WHERE id=%s',(row['outbox_id'],)).fetchone()
                delivery_unknown=outbox is not None and outbox['status']=='delivery_unknown'
            is_exhausted=row['attempt']>=max_attempts and not delivery_unknown
            status='failed' if delivery_unknown or is_exhausted else 'retry_wait'
            updated=self.connection.execute("""UPDATE jobs SET status=%s,lease_owner=NULL,lease_until=NULL,next_attempt_at=%s
              WHERE id=%s AND status='running' AND fence_token=%s AND lease_until<=%s""",
              (status,now,row['id'],row['fence_token'],now)).rowcount
            if updated:
                if is_exhausted:
                    exhausted.append(row['id'])
                elif status=='retry_wait':
                    requeued.append(row['id'])
        return c.RecoveryReport(requeued_ids=tuple(requeued),exhausted_ids=tuple(exhausted),
            unknown_delivery_ids=unknown,cancelled_ids=(),stale_claims=())



class OutboxRepository(EncryptedRepository):
    def append_unique(self,dto):
        self._write(dto,conflict=True)
        values=metadata(dto)
        return self._decode(self._one('bot_scope=%s AND owner_id=%s AND case_id IS NOT DISTINCT FROM %s AND dedupe_key=%s',
            (values['bot_scope'],values['owner_id'],values['case_id'],values['dedupe_key'])))
    def begin_send_if_allowed(self,claim,permit,now):
        if not self.uow.work.is_claim_current(claim,now):
            return failure('LEASE_LOST')
        if claim.kind!='deliver_outbox' or claim.payload_ref!=permit.outbox_id or permit.job_id!=claim.job_id or permit.fence_token!=claim.fence_token or permit.expires_at<=now:
            return failure('ACCESS_DENIED')
        row=self._one('id=%s AND owner_id=%s',(permit.outbox_id,permit.owner_id),True)
        if row is None or row['status'] not in ('pending','preparing','retry_wait'):
            return failure('INVALID_TRANSITION')
        record=self._decode(row)
        if claim.owner_id!=permit.owner_id or record.intent.case_guard!=permit.case_guard or permit.payload_hash!=c.content_hash(record.payload):
            return failure('ACCESS_DENIED')
        if record.intent.delivery_target_id!=permit.delivery_target_id:
            return failure('ACCESS_DENIED')
        if record.intent.case_guard is not None:
            case=self.connection.execute('SELECT * FROM cases WHERE id=%s AND owner_id=%s FOR UPDATE',(record.intent.case_guard.case_id,permit.owner_id)).fetchone()
            guard=record.intent.case_guard
            if case is None or case['status']=='deleted':
                return failure('CASE_DELETED')
            if case['revision']!=guard.expected_revision or case['deletion_epoch']!=guard.expected_deletion_epoch:
                return failure('STALE_REVISION')
        elif record.intent.kind=='material':
            return failure('ACCESS_DENIED')
        record=record.model_copy(update={'status':'sending','send_permit':permit})
        self.save(record)
        blob=self.uow.crypto.encrypt(permit.model_dump_json().encode())
        self.connection.execute('INSERT INTO send_attempts(id,outbox_id,job_id,fence_token,status,key_id,nonce,ciphertext,tag) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (permit.send_attempt_id,permit.outbox_id,claim.job_id,claim.fence_token,'sending',blob.key_id,blob.nonce,blob.ciphertext,blob.tag))
        return success(permit)
    def record_transport_result(self,claim,result,now):
        if not self.uow.work.is_claim_current(claim,now):
            return False
        row=self._one("id=%s AND status='sending'",(claim.payload_ref,),True)
        if row is None:
            return False
        record=self._decode(row)
        if record is None:
            return False
        permit=record.send_permit
        if permit is None or permit.job_id!=claim.job_id or permit.fence_token!=claim.fence_token:
            return False
        variant=getattr(result,'status',getattr(result,'kind',None))
        if variant=='confirmed':
            receipt=result.receipt
            if record.intent.kind=='callback_answer':
                if (not isinstance(record.payload,c.CallbackAnswer)
                    or not isinstance(receipt,c.CallbackReceipt)
                    or receipt.operation!='answer_callback'
                    or receipt.callback_id!=record.payload.platform_callback_id
                    or record.payload.owner_id!=record.owner_id):
                    return False
            elif record.intent.kind in ('view','material'):
                # Current intents represent new sends. An edit requires a separate,
                # explicit known-message binding before accepting edit receipts.
                if not isinstance(receipt,c.MessageReceipt) or receipt.operation!='send_message' or not receipt.message_id:
                    return False
            else:
                return False
        status={'confirmed':'confirmed','definitely_rejected':'definitely_rejected','unknown':'delivery_unknown'}.get(variant)
        if status is None:
            raise ValueError('Invalid transport outcome')
        if variant=='definitely_rejected' and getattr(result,'retryable',False):
            status='retry_wait'
        self.save(record.model_copy(update={'status':status,'transport_result':result}))
        self.connection.execute('UPDATE send_attempts SET status=%s WHERE id=%s AND fence_token=%s',(status,permit.send_attempt_id,claim.fence_token))
        return True
    def _recover_sending_for_jobs(self,job_ids):
        # Caller holds the job locks before changing outbox state, matching send CAS lock order.
        if not job_ids:
            return c.RecoveryReport()
        rows=self.connection.execute("""UPDATE outbox_messages SET status='delivery_unknown' WHERE status='sending' AND id IN
          (SELECT outbox_id FROM jobs WHERE id=ANY(%s)) RETURNING id""",(list(job_ids),)).fetchall()
        for row in rows:
            self.connection.execute("UPDATE send_attempts SET status='delivery_unknown' WHERE outbox_id=%s AND status='sending'",(row['id'],))
        return c.RecoveryReport(unknown_delivery_ids=tuple(row['id'] for row in rows))
    def recover_sending(self,now,bot_scope=None):
        rows=self.connection.execute("""SELECT id FROM jobs WHERE kind='deliver_outbox'
          AND (status<>'running' OR lease_until<=%s) AND (%s::text IS NULL OR bot_scope=%s)
          ORDER BY id FOR UPDATE SKIP LOCKED""",(now,bot_scope,bot_scope)).fetchall()
        return self._recover_sending_for_jobs(tuple(row['id'] for row in rows))



class Database:
    def __init__(self,dsn:str,crypto:CryptoPort):
        self.dsn,self.crypto=dsn,crypto
    def migrate(self):
        migration_root=Path(__file__).resolve().parents[4]/'migrations'
        with psycopg.connect(self.dsn) as connection:
            # Serializes independent HTTP/worker startup migration attempts.
            connection.execute('SELECT pg_advisory_xact_lock(193765018)')
            for path in sorted(migration_root.glob('*.sql')):
                connection.execute(path.read_text())
    def list_cleanup_requests(self):
        with self.uow() as unit:
            rows=unit.connection.execute("SELECT case_id,deletion_epoch FROM cleanup_requests WHERE status='pending' ORDER BY case_id").fetchall()
            return tuple(c.CaseCleanupRecord(**row) for row in rows)
    def complete_case_cleanup(self,case_id,deletion_epoch):
        with self.uow() as unit:
            row=unit.connection.execute('SELECT deletion_epoch FROM case_tombstones WHERE case_id=%s FOR UPDATE',(case_id,)).fetchone()
            if row is None or row['deletion_epoch']!=deletion_epoch:
                return False
            # Keep minimal IDs/FK/dedupe/tombstone rows, erase all sensitive DTO payload.
            for table in ('cases','input_revisions','input_candidates','result_previews','confirmations','document_manifests','document_bundles','document_artifacts','callback_handles','comparisons','inbox_events','outbox_messages','uploaded_attachments','jobs'):
                column='id' if table=='cases' else 'case_id'
                unit.connection.execute(sql.SQL("UPDATE {} SET ciphertext=''::bytea,nonce=''::bytea,tag=''::bytea WHERE {}=%s").format(sql.Identifier(table),sql.Identifier(column)),(case_id,))
            unit.connection.execute("UPDATE send_attempts SET ciphertext=''::bytea,nonce=''::bytea,tag=''::bytea WHERE outbox_id IN (SELECT id FROM outbox_messages WHERE case_id=%s)",(case_id,))
            unit.connection.execute("UPDATE cleanup_requests SET status='completed' WHERE case_id=%s AND deletion_epoch=%s",(case_id,deletion_epoch))
            unit.commit()
            return True
    def ping(self):
        try:
            with psycopg.connect(self.dsn,connect_timeout=3) as connection:
                return connection.execute('SELECT 1').fetchone()[0]==1
        except psycopg.Error:
            return False
    @contextmanager
    def uow(self):
        connection=psycopg.connect(self.dsn,row_factory=dict_row)
        unit=UnitOfWork(connection,self.crypto)
        try:
            yield unit
        finally:
            # Repositories never commit. Uncommitted writes always roll back.
            connection.rollback()
            connection.close()


class UnitOfWork:
    def __init__(self,connection,crypto):
        self.connection,self.crypto=connection,crypto
        self.cases=CaseRepository(self,'cases',c.CaseSnapshot)
        self.inputs=InputRepository(self,'input_revisions',c.InputRevision,True)
        self.candidates=EncryptedRepository(self,'input_candidates',c.Candidate,True)
        self.previews=EncryptedRepository(self,'result_previews',c.ResultPreview,True)
        self.comparisons=EncryptedRepository(self,'comparisons',c.ComparisonRecord,True)
        self.confirmations=EncryptedRepository(self,'confirmations',c.Confirmation,True)
        self.manifests=EncryptedRepository(self,'document_manifests',c.DocumentManifest,True)
        self.bundles=EncryptedRepository(self,'document_bundles',c.DocumentBundle)
        self.artifacts=EncryptedRepository(self,'document_artifacts',c.ArtifactRecord)
        self.handles=HandleRepository(self,'callback_handles',c.HandleRecord,True)
        self.inbox=InboxRepository(self,'inbox_events',c.InboxRecord)
        self.attachments=AttachmentRepository(self,'uploaded_attachments',c.AttachmentRecord,True)
        self.identities=IdentityRepository(self,'user_identities',c.IdentityRecord)
        self.outbox=OutboxRepository(self,'outbox_messages',c.OutboxRecord)
        self.work=WorkRepository(self)
        self.releases=ReleaseRepository(self)
    def savepoint(self,name):
        self.connection.execute(sql.SQL('SAVEPOINT {}').format(sql.Identifier(name)))
    def rollback_to_savepoint(self,name):
        self.connection.execute(sql.SQL('ROLLBACK TO SAVEPOINT {}').format(sql.Identifier(name)))
    def release_savepoint(self,name):
        self.connection.execute(sql.SQL('RELEASE SAVEPOINT {}').format(sql.Identifier(name)))
    def commit(self):
        self.connection.commit()
    def rollback(self):
        self.connection.rollback()

class ReleaseRepository:
    """Public validated content; lifecycle and active pointer are distinct rows."""
    def __init__(self,uow):
        self.connection=uow.connection
    def insert(self,record):
        if record.ref!=record.manifest.ref:
            raise ValueError('VALIDATION_ERROR')
        from psycopg.types.json import Jsonb
        self.connection.execute('INSERT INTO data_releases(id,version,content,content_hash) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',
            (record.ref.id,record.ref.version,Jsonb(record.model_dump(mode='json')),record.content_hash))
        saved=self.get(record.ref)
        if saved!=record:
            raise ValueError('Immutable release collision')
        due=record.manifest.review.review_due_at
        self.connection.execute('INSERT INTO data_lifecycle(id,version,review_due_at) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(record.ref.id,record.ref.version,due))
        return saved
    def get(self,ref):
        row=self.connection.execute('SELECT content FROM data_releases WHERE id=%s AND version=%s',(ref.id,ref.version)).fetchone()
        return c.ReleaseRecord.model_validate(row['content']) if row else None
    def read_lifecycle(self,ref,lock=False):
        row=self.connection.execute('SELECT * FROM data_lifecycle WHERE id=%s AND version=%s'+(' FOR UPDATE' if lock else ''),(ref.id,ref.version)).fetchone()
        return c.ReleaseLifecycle(ref=ref,revoked=row['revoked'],review_due_at=row['review_due_at'],reason_code=row['reason_code']) if row else None
    def get_active(self,mode,lock=False):
        row=self.connection.execute('SELECT release_id,release_version FROM active_releases WHERE mode=%s'+(' FOR UPDATE' if lock else ''),(mode,)).fetchone()
        return self.get(c.VersionRef(id=row['release_id'],version=row['release_version'])) if row else None
    def _audit(self,ref,actor_key,now,result):
        self.connection.execute('INSERT INTO source_audit(id,subject_id,actor_key,occurred_at,result_code) VALUES(%s,%s,%s,%s,%s)',(uuid4(),ref.id+':'+ref.version,actor_key,now,result))
    def activate(self,ref,mode,actor_key,now,allow_synthetic_draft=False):
        if mode not in ('demo','pilot'):
            raise ValueError('VALIDATION_ERROR')
        record=self.get(ref)
        if record is None:
            raise ValueError('DATA_NOT_READY')
        lifecycle=self.connection.execute('SELECT * FROM data_lifecycle WHERE id=%s AND version=%s FOR UPDATE',(ref.id,ref.version)).fetchone()
        if lifecycle is None:
            raise ValueError('DATA_NOT_READY')
        if lifecycle['revoked']:
            raise ValueError('DATA_REVOKED')
        if lifecycle['review_due_at'] is not None and lifecycle['review_due_at']<=now:
            raise ValueError('DATA_EXPIRED')
        draft=record.manifest.data_kind=='synthetic' and record.manifest.review.status=='draft'
        if draft:
            if mode!='demo' or not allow_synthetic_draft or not record.assets_verified or not record.package_reviews or not record.package_data_kinds:
                raise ValueError('DATA_NOT_READY')
            if any(kind!='synthetic' for kind in record.package_data_kinds) or any(review.status!='draft' for review in record.package_reviews):
                raise ValueError('DATA_NOT_READY')
        else:
            reviews=(record.manifest.review,*record.package_reviews)
            if record.manifest.data_kind!='public_snapshot' or not record.package_reviews or not record.package_data_kinds or not record.assets_verified:
                raise ValueError('DATA_NOT_READY')
            if any(kind!='public_snapshot' for kind in record.package_data_kinds):
                raise ValueError('DATA_NOT_READY')
            if any(review.status!='reviewed' or review.reviewed_at is None or review.review_due_at is None or not review.reviewed_at<=now<review.review_due_at for review in reviews):
                raise ValueError('DATA_NOT_READY')
        self.connection.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',('tsr-release:'+mode,))
        old=self.get_active(mode)
        self.connection.execute('INSERT INTO active_releases(mode,release_id,release_version) VALUES(%s,%s,%s) ON CONFLICT(mode) DO UPDATE SET release_id=excluded.release_id,release_version=excluded.release_version',(mode,ref.id,ref.version))
        self._audit(ref,actor_key,now,'ACTIVATED')
        return c.ActivationReceipt(previous_ref=old.ref if old else None,active_ref=ref,activated_at=now,actor_key=actor_key)
    def revoke(self,ref,reason,actor_key,now):
        result=self.connection.execute('UPDATE data_lifecycle SET revoked=true,reason_code=%s WHERE id=%s AND version=%s',(reason,ref.id,ref.version))
        if not result.rowcount:
            raise ValueError('NOT_FOUND')
        self._audit(ref,actor_key,now,'REVOKED')
        return self.read_lifecycle(ref)
