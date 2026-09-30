from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generic, TypeVar
from uuid import UUID, uuid4
import hmac
import secrets
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
        for field,column in (('case_revision','revision'),('deletion_epoch','deletion_epoch')):
            if field in self.model.model_fields and row.get(column) is not None:
                dto=dto.model_copy(update={field:row[column]})
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

    def _metadata(self,dto):
        values=metadata(dto)
        if not values['bot_scope'] and values['owner_id'] is not None:
            identity=self.connection.execute('SELECT bot_scope FROM user_identities WHERE owner_id=%s ORDER BY id LIMIT 1',(values['owner_id'],)).fetchone()
            if identity is not None:
                values['bot_scope']=identity['bot_scope']
        values['created_at']=getattr(dto,'created_at',None) or getattr(dto,'received_at',None) or getattr(getattr(dto,'intent',None),'created_at',None) or datetime.now(timezone.utc)
        values['updated_at']=datetime.now(timezone.utc)
        if self.table=='cases':
            values['last_activity_at']=dto.last_activity_at
        if self.table=='comparisons':
            values['comparison_input_revision_id']=dto.result.input_revision_id
            values['comparison_snapshot_id']=dto.result.snapshot_id
        if self.table=='user_identities':
            values['delivery_target_id']=dto.delivery_target_id
        if self.table=='uploaded_attachments':
            values['manifest_hash']=dto.manifest_hash
            values['observed_at']=dto.observed_at
        if self.table in ('input_candidates','callback_handles'):
            values['dialog_revision']=dto.dialog_revision
        if self.table=='callback_handles':
            values['action_command_type']=dto.action.command_type
        if self.table=='outbox_messages':
            values['dialog_revision']=dto.payload.dialog_revision if isinstance(dto.payload,c.ViewModel) else None
            values['payload_kind']='view' if isinstance(dto.payload,c.ViewModel) else dto.intent.kind if dto.payload is not None else 'none'
            values['delivery_target_id']=dto.intent.delivery_target_id
            receipt=dto.transport_result.receipt if dto.transport_result is not None else None
            values['receipt_digest']=self.uow._receipt_digest(dto.owner_id,dto.intent.delivery_target_id,receipt) if receipt is not None else None
        return values

    def _write(self,dto,update=False,conflict=False):
        values=self._metadata(dto)|self._encrypted(dto)
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
            update_columns=[k for k in columns if k not in ('id','created_at')]
            assignments=sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(k)) for k in update_columns)
            query=sql.SQL('UPDATE {} SET {} WHERE id=%s AND owner_id=%s').format(sql.Identifier(self.table),assignments)
            result=self.connection.execute(query,tuple(values[k] for k in update_columns)+(values['id'],values['owner_id']))
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

    def _page(self,where,args,limit,offset=0,*,order='created_at'):
        if (type(limit) is not int or not 1<=limit<=100 or type(offset) is not int
                or not 0<=offset<=1_000_000 or order not in ('created_at','last_activity_at')):
            raise ValueError('Invalid bounded page')
        query=sql.SQL('SELECT * FROM {} WHERE '+where+' ORDER BY {} DESC,id DESC LIMIT %s OFFSET %s').format(
            sql.Identifier(self.table),sql.Identifier(order))
        rows=self.connection.execute(query,tuple(args)+(limit,offset)).fetchall()
        return tuple(dto for row in rows if (dto:=self._decode(row)) is not None)

    def _count(self,where,args):
        return self.connection.execute(sql.SQL('SELECT count(*) AS count FROM {} WHERE '+where).format(
            sql.Identifier(self.table)),args).fetchone()['count']

    def count_live_by_case(self,case_id):
        if self.table!='document_artifacts':raise ValueError('Unsupported page repository')
        return self._count("case_id=%s AND status='published' AND octet_length(ciphertext)>0",(case_id,))

    def page_live_by_case(self,case_id,limit,offset=0):
        if self.table!='document_artifacts':raise ValueError('Unsupported page repository')
        return self._page("case_id=%s AND status='published' AND octet_length(ciphertext)>0",(case_id,),limit,offset)

    def latest_by_case(self,case_id,limit=3):
        if self.table!='document_bundles':raise ValueError('Unsupported page repository')
        return self._page("case_id=%s AND status<>'deleted' AND octet_length(ciphertext)>0",(case_id,),limit)

    def find_for_guard(self,owner_id,guard,input_revision_id,snapshot_ids):
        if self.table!='comparisons' or len(snapshot_ids)>3:
            raise ValueError('Invalid comparison lookup')
        if not snapshot_ids:return ()
        rows=self.connection.execute("""SELECT DISTINCT ON (comparison_snapshot_id) * FROM comparisons
          WHERE owner_id=%s AND case_id=%s AND revision=%s AND deletion_epoch=%s
            AND comparison_input_revision_id=%s AND comparison_snapshot_id=ANY(%s)
            AND octet_length(ciphertext)>0
          ORDER BY comparison_snapshot_id,created_at DESC,id DESC LIMIT 3""",
          (owner_id,guard.case_id,guard.expected_revision,guard.expected_deletion_epoch,input_revision_id,list(snapshot_ids))).fetchall()
        return tuple(dto for row in rows if (dto:=self._decode(row)) is not None)

    def find_by_manifest(self,manifest_id):
        return self._decode(self._one('manifest_id=%s',(manifest_id,)))

    def find_by_preview(self,preview_id):
        return self._decode(self._one('preview_id=%s',(preview_id,)))

    def latest_for_guard(self,owner_id,guard,now,dialog_revision=None):
        if self.table!='input_candidates':raise ValueError('Unsupported candidate lookup')
        where='owner_id=%s AND case_id=%s AND revision=%s AND deletion_epoch=%s AND active AND expires_at>%s AND octet_length(ciphertext)>0'
        args=(owner_id,guard.case_id,guard.expected_revision,guard.expected_deletion_epoch,now)
        if dialog_revision is not None:
            where+=' AND dialog_revision=%s';args+=(dialog_revision,)
        page=self._page(where,args,1)
        return page[0] if page else None

    def find_by_confirmation(self,confirmation_id,owner_id,case_id):
        if self.table!='document_manifests':raise ValueError('Unsupported manifest lookup')
        return self._decode(self._one('parent_id=%s AND owner_id=%s AND case_id=%s',(confirmation_id,owner_id,case_id)))

    def get_many(self,ids,owner_id,case_id):
        if self.table!='document_artifacts' or len(ids)>4:raise ValueError('Invalid bounded artifact lookup')
        if not ids:return ()
        rows=self.connection.execute("SELECT * FROM document_artifacts WHERE id=ANY(%s) AND owner_id=%s AND case_id=%s AND status='published' AND octet_length(ciphertext)>0 ORDER BY id",(list(ids),owner_id,case_id)).fetchall()
        return tuple(dto for row in rows if (dto:=self._decode(row)) is not None)

    def mark_historical(self,case_id,owner_id):
        if self.table!='document_bundles':raise ValueError('Unsupported bundle transition')
        return self.connection.execute("UPDATE document_bundles SET status='historical',updated_at=now() WHERE case_id=%s AND owner_id=%s AND status NOT IN ('historical','deleted','expired') AND octet_length(ciphertext)>0",(case_id,owner_id)).rowcount


class CaseRepository(EncryptedRepository):
    def count_live_by_owner(self,owner_id):
        return self._count("owner_id=%s AND status NOT IN ('deleted','deleting') AND octet_length(ciphertext)>0",(owner_id,))

    def page_live_by_owner(self,owner_id,limit,offset=0):
        return self._page("owner_id=%s AND status NOT IN ('deleted','deleting') AND octet_length(ciphertext)>0",(owner_id,),limit,offset,order='last_activity_at')

    def latest_live_by_owner(self,owner_id):
        page=self.page_live_by_owner(owner_id,1)
        return page[0] if page else None

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
        from tsr.domain.cases import mark_case_deleted
        epoch=mark_case_deleted(result.value,now).new_epoch
        case=result.value.model_copy(update={'status':'deleted','deletion_epoch':epoch})
        self.save(case)
        self.connection.execute('INSERT INTO case_tombstones(case_id,owner_id,deletion_epoch,deleted_at) VALUES(%s,%s,%s,%s) ON CONFLICT(case_id) DO NOTHING',(guard.case_id,ctx.owner_id,epoch,now))
        self.connection.execute('INSERT INTO cleanup_requests(case_id,deletion_epoch) VALUES(%s,%s) ON CONFLICT(case_id) DO NOTHING',(guard.case_id,epoch))
        scope=self.connection.execute('SELECT bot_scope FROM cases WHERE id=%s',(guard.case_id,)).fetchone()['bot_scope']
        self.connection.execute('INSERT INTO deletion_journal(case_id,owner_id,deletion_epoch,deleted_at,bot_scope) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(case_id) DO UPDATE SET deletion_epoch=GREATEST(deletion_journal.deletion_epoch,excluded.deletion_epoch)',(guard.case_id,ctx.owner_id,epoch,now,scope))
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
        return self.find_by_target(owner_id,target_id)

    def find_by_target(self,owner_id,target_id):
        rows=self.connection.execute('SELECT * FROM user_identities WHERE owner_id=%s AND delivery_target_id=%s AND octet_length(ciphertext)>0 ORDER BY id LIMIT 2',(owner_id,target_id)).fetchall()
        if len(rows)!=1:return None
        record=self._decode(rows[0])
        return record if record.owner_id==owner_id and record.delivery_target_id==target_id else None

    def for_owner(self,owner_id,limit=1):
        if type(limit) is not int or not 1<=limit<=2:raise ValueError('Invalid bounded identity lookup')
        rows=self.connection.execute('SELECT * FROM user_identities WHERE owner_id=%s AND octet_length(ciphertext)>0 ORDER BY id LIMIT %s',(owner_id,limit)).fetchall()
        return tuple(dto for row in rows if (dto:=self._decode(row)) is not None)

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
    def mark_processed(self,id,status='processed',now=None):
        if status not in ('processed','ignored','failed','received','processing'):
            raise ValueError('Invalid inbox status')
        observed=now or datetime.now(timezone.utc)
        self.connection.execute('UPDATE inbox_events SET status=%s,processed_at=%s,updated_at=%s WHERE id=%s',(status,observed if status in ('processed','ignored','failed') else None,observed,id))


class HandleRepository(EncryptedRepository):
    def find(self,owner_id,case_id,opaque_handle):
        # Read the exact bound action even after callback TTL; resolve remains
        # the guarded consume/authorization port.
        return self._decode(self._one('owner_id=%s AND case_id IS NOT DISTINCT FROM %s AND opaque_key=%s',(owner_id,case_id,opaque_handle)))

    def invalidate_owner(self,owner_id,case_id=None):
        where="owner_id=%s AND active AND case_id IS NULL AND action_command_type='start_case'" if case_id is None else 'owner_id=%s AND active AND case_id=%s'
        args=(owner_id,) if case_id is None else (owner_id,case_id)
        return self.connection.execute('UPDATE callback_handles SET active=false WHERE '+where,args).rowcount

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
        row=self.connection.execute("""SELECT * FROM uploaded_attachments
          WHERE owner_id=%s AND artifact_id=%s AND manifest_hash=%s AND octet_length(ciphertext)>0
          ORDER BY observed_at DESC,id DESC LIMIT 1""",(owner_id,artifact_id,manifest_hash)).fetchone()
        record=self._decode(row)
        return record if (record is not None and record.owner_id==owner_id
            and record.artifact_id==artifact_id and record.manifest_hash==manifest_hash) else None


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
    def cancel_old_for_case(self,case_id,owner_id,current_revision):
        # Lock jobs before a send can begin. Durable sending/unknown outcomes
        # retain their original claim so terminal transport results can record.
        rows=self.connection.execute("""SELECT id FROM jobs WHERE case_id=%s AND owner_id=%s
          AND revision<>%s AND status IN ('queued','retry_wait','running') ORDER BY id FOR UPDATE""",
          (case_id,owner_id,current_revision)).fetchall()
        if not rows:return 0
        return self.connection.execute("""UPDATE jobs j SET status='cancelled',fence_token=fence_token+1,
          lease_until=NULL,lease_owner=NULL,updated_at=now()
          WHERE j.id=ANY(%s) AND j.status IN ('queued','retry_wait','running')
          AND NOT EXISTS(SELECT 1 FROM outbox_messages o WHERE o.id=j.outbox_id AND o.status IN ('sending','delivery_unknown'))""",
          ([row['id'] for row in rows],)).rowcount
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
          UPDATE jobs SET status='running',fence_token=fence_token+1,lease_owner=%s,lease_until=%s,claimed_at=%s,updated_at=%s,attempt=attempt+1
          WHERE id=(SELECT id FROM candidate) RETURNING *""",(kind,now,bot_scope,bot_scope,worker_id,now+timedelta(seconds=lease_seconds),now,now)).fetchone()
        if row is None:
            return None
        record=self._decode(row)
        return c.ClaimedJob(**record.work.model_dump(),fence_token=record.fence_token,lease_owner=record.lease_owner,
            lease_until=record.lease_until,attempt=record.attempt,next_attempt_at=record.next_attempt_at,trace_id=record.trace_id,created_at=row.get('created_at'))
    def is_claim_current(self,claim,now):
        return self.connection.execute("SELECT 1 FROM jobs WHERE id=%s AND status='running' AND fence_token=%s AND lease_owner=%s AND lease_until>%s FOR UPDATE",(claim.job_id,claim.fence_token,claim.lease_owner,now)).fetchone() is not None
    check_claim=is_claim_current
    def _cas(self,claim,now,assignment,values):
        query="UPDATE jobs SET "+assignment+",updated_at=%s WHERE id=%s AND status='running' AND fence_token=%s AND lease_owner=%s AND lease_until>%s"
        return self.connection.execute(query,tuple(values)+(now,claim.job_id,claim.fence_token,claim.lease_owner,now)).rowcount==1
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
    def latest_question(self,owner_id,case_id,dialog_revision):
        page=self._page("owner_id=%s AND case_id=%s AND dialog_revision=%s AND payload_kind='view' AND octet_length(ciphertext)>0",(owner_id,case_id,dialog_revision),1)
        return page[0] if page else None

    def latest_for_owner_case(self,owner_id,case_id):
        page=self._page('owner_id=%s AND case_id=%s AND octet_length(ciphertext)>0',(owner_id,case_id),1)
        return page[0] if page else None

    def receipt_exists(self,owner_id,target_id,receipt):
        if not isinstance(receipt,(c.MessageReceipt,c.CallbackReceipt)):return False
        digest=self.uow._receipt_digest(owner_id,target_id,receipt)
        rows=self.connection.execute("SELECT * FROM outbox_messages WHERE owner_id=%s AND delivery_target_id=%s AND receipt_digest=%s AND status='confirmed' AND octet_length(ciphertext)>0 ORDER BY id LIMIT 2",(owner_id,target_id,digest)).fetchall()
        if len(rows)!=1:return False
        record=self._decode(rows[0])
        return (record is not None and record.owner_id==owner_id and record.intent.owner_id==owner_id
            and record.intent.delivery_target_id==target_id and record.transport_result is not None
            and type(record.transport_result.receipt) is type(receipt) and record.transport_result.receipt==receipt)

    def latest_unknown_by_case(self,case_id,limit=3):
        return self._page("case_id=%s AND status='delivery_unknown' AND octet_length(ciphertext)>0",(case_id,),limit)

    def append_unique(self,dto):
        self._write(dto,conflict=True)
        values=self._metadata(dto)
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
    def __init__(self,dsn:str,crypto:CryptoPort,*,connect_timeout_seconds=5,statement_timeout_ms=3000,
                 lock_timeout_ms=1000,idle_transaction_timeout_ms=5000,backup_snapshot_timeout_ms=300000):
        limits=(connect_timeout_seconds,statement_timeout_ms,lock_timeout_ms,idle_transaction_timeout_ms,backup_snapshot_timeout_ms)
        if any(value<1 for value in limits):
            raise ValueError('Database deadlines must be positive')
        self.dsn,self.crypto=dsn,crypto
        self.connect_timeout_seconds=connect_timeout_seconds
        self.statement_timeout_ms,self.lock_timeout_ms=statement_timeout_ms,lock_timeout_ms
        self.idle_transaction_timeout_ms,self.backup_snapshot_timeout_ms=idle_transaction_timeout_ms,backup_snapshot_timeout_ms
    def _connect(self,backup=False):
        connection=psycopg.connect(self.dsn,row_factory=dict_row,connect_timeout=self.connect_timeout_seconds)
        try:
            statement=self.backup_snapshot_timeout_ms if backup else self.statement_timeout_ms
            idle=self.backup_snapshot_timeout_ms if backup else self.idle_transaction_timeout_ms
            connection.execute("SELECT set_config('statement_timeout',%s,false),set_config('lock_timeout',%s,false),set_config('idle_in_transaction_session_timeout',%s,false)",(str(statement),str(self.lock_timeout_ms),str(idle)))
            connection.commit()
            return connection
        except BaseException:
            connection.close()
            raise
    def migrate(self):
        migration_root=Path(__file__).resolve().parents[4]/'migrations'
        with self._connect() as connection:
            # Serializes independent HTTP/worker startup migration attempts.
            connection.execute('SELECT pg_advisory_xact_lock(193765018)')
            for path in sorted(migration_root.glob('*.sql')):
                connection.execute(path.read_text())
            if connection.execute('SELECT 1 FROM private_index_keys WHERE id=1').fetchone() is None:
                blob=self.crypto.encrypt(secrets.token_bytes(32))
                connection.execute('INSERT INTO private_index_keys(id,key_id,nonce,ciphertext,tag) VALUES(1,%s,%s,%s,%s)',(blob.key_id,blob.nonce,blob.ciphertext,blob.tag))
            self._backfill_metadata(UnitOfWork(connection,self.crypto))
    def _backfill_metadata(self,unit):
        for row in unit.connection.execute("SELECT * FROM cases WHERE last_activity_at IS NULL AND octet_length(ciphertext)>0").fetchall():
            case=unit.cases._decode(row)
            unit.connection.execute('UPDATE cases SET last_activity_at=%s WHERE id=%s',(case.last_activity_at,case.case_id))
        for table in ('cases','input_revisions','input_candidates','result_previews','confirmations','document_manifests','document_bundles','document_artifacts','callback_handles','comparisons','outbox_messages','uploaded_attachments'):
            unit.connection.execute(sql.SQL("UPDATE {} t SET bot_scope=i.bot_scope FROM user_identities i WHERE t.owner_id=i.owner_id AND t.bot_scope=''").format(sql.Identifier(table)))
        # Migration metadata does not alter the immutable encrypted comparisons.
        for row in unit.connection.execute("SELECT * FROM comparisons WHERE comparison_input_revision_id IS NULL AND octet_length(ciphertext)>0").fetchall():
            record=unit.comparisons._decode(row)
            unit.connection.execute('UPDATE comparisons SET comparison_input_revision_id=%s,comparison_snapshot_id=%s WHERE id=%s',
                (record.result.input_revision_id,record.result.snapshot_id,record.comparison_id))
        for repo,where,columns in (
            (unit.identities,'delivery_target_id IS NULL',('delivery_target_id',)),
            (unit.attachments,'manifest_hash IS NULL OR observed_at IS NULL',('manifest_hash','observed_at')),
            (unit.candidates,'dialog_revision IS NULL',('dialog_revision',)),
            (unit.handles,'action_command_type IS NULL',('dialog_revision','action_command_type')),
            (unit.outbox,"payload_kind IS NULL OR (status='confirmed' AND receipt_digest IS NULL)",('dialog_revision','payload_kind','delivery_target_id','receipt_digest')),
        ):
            rows=unit.connection.execute(sql.SQL('SELECT * FROM {} WHERE ('+where+') AND octet_length(ciphertext)>0').format(sql.Identifier(repo.table))).fetchall()
            for row in rows:
                values=repo._metadata(repo._decode(row))
                assignments=sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(column)) for column in columns)
                unit.connection.execute(sql.SQL('UPDATE {} SET {} WHERE id=%s').format(sql.Identifier(repo.table),assignments),
                    tuple(values[column] for column in columns)+(row['id'],))
        unit.connection.execute("UPDATE deletion_journal j SET bot_scope=c.bot_scope FROM cases c WHERE j.case_id=c.id AND j.bot_scope='' AND c.bot_scope<>''")
    def list_cleanup_requests(self,bot_scope=None):
        with self.uow() as unit:
            rows=unit.connection.execute("SELECT r.case_id,r.deletion_epoch FROM cleanup_requests r JOIN cases c ON c.id=r.case_id WHERE r.status='pending' AND (%s::text IS NULL OR c.bot_scope=%s) ORDER BY r.case_id",(bot_scope,bot_scope)).fetchall()
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
    def record_worker_heartbeat(self,worker_id,now,release_commit,bot_scope='default',capacity=None):
        from psycopg.types.json import Jsonb
        record=c.WorkerHeartbeat(worker_id=worker_id,bot_scope=bot_scope,release_commit=release_commit,heartbeat_at=now,capacity=capacity or {})
        with self.uow() as unit:
            unit.connection.execute('INSERT INTO worker_heartbeats(worker_id,bot_scope,release_commit,heartbeat_at,capacity) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(worker_id,bot_scope) DO UPDATE SET release_commit=excluded.release_commit,heartbeat_at=excluded.heartbeat_at,capacity=excluded.capacity WHERE excluded.heartbeat_at>=worker_heartbeats.heartbeat_at',
                (record.worker_id,record.bot_scope,record.release_commit,record.heartbeat_at,Jsonb(dict(record.capacity))))
            unit.commit()
    def record_subscription_health(self,bot_scope,health):
        with self.uow() as unit:
            unit.connection.execute('INSERT INTO subscription_health(bot_scope,status,checked_at,reason_code,secret_verified) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(bot_scope) DO UPDATE SET status=excluded.status,checked_at=excluded.checked_at,reason_code=excluded.reason_code,secret_verified=excluded.secret_verified WHERE excluded.checked_at>=subscription_health.checked_at',
                (bot_scope,health.status,health.checked_at,health.reason_code,health.secret_verified))
            unit.commit()
    def read_subscription_health(self,bot_scope):
        with self.uow() as unit:
            row=unit.connection.execute('SELECT * FROM subscription_health WHERE bot_scope=%s',(bot_scope,)).fetchone()
            return c.SubscriptionHealth(**row) if row else None
    def admit_quota(self,bot_scope,owner_id,kind,now,limit,window_seconds):
        if limit<1 or window_seconds<1:
            raise ValueError('Invalid quota')
        window=int(now.timestamp())//window_seconds*window_seconds
        with self.uow() as unit:
            row=unit.connection.execute('INSERT INTO quota_counters(bot_scope,owner_id,kind,window_start,expires_at,used) VALUES(%s,%s,%s,%s,%s,1) ON CONFLICT(bot_scope,owner_id,kind,window_start) DO UPDATE SET used=quota_counters.used+1 WHERE quota_counters.used<%s RETURNING used',
                (bot_scope,owner_id or UUID(int=0),kind,window,datetime.fromtimestamp(window+window_seconds,timezone.utc),limit)).fetchone()
            unit.commit()
            return row is not None
    def collect_health(self,now,scope,release_ref=None,*,mode='demo',worker_heartbeat_seconds=60):
        with self.uow() as unit:
            heartbeats=unit.connection.execute('SELECT release_commit,heartbeat_at FROM worker_heartbeats WHERE bot_scope=%s ORDER BY heartbeat_at DESC',(scope,)).fetchall()
            latest=heartbeats[0] if heartbeats else None
            age=max(0,(now-latest['heartbeat_at']).total_seconds()) if latest else None
            recent=[row for row in heartbeats if 0<=(now-row['heartbeat_at']).total_seconds()<worker_heartbeat_seconds]
            rows=unit.connection.execute("SELECT kind,status,count(*) AS count,min(CASE WHEN status='running' THEN claimed_at ELSE created_at END) AS oldest,min(created_at) AS total_oldest FROM jobs WHERE bot_scope=%s AND status IN ('queued','retry_wait','running') GROUP BY kind,status",(scope,)).fetchall()
            queue=[];counts={};oldest=[]
            for kind in ('process_inbox','render_artifact','deliver_outbox'):
                pending=[row for row in rows if row['kind']==kind and row['status'] in ('queued','retry_wait')]
                running=[row for row in rows if row['kind']==kind and row['status']=='running']
                def delay(group):
                    known=[row['oldest'] for row in group if row['oldest'] is not None]
                    return max(0,(now-min(known)).total_seconds()) if known else None
                queued_age,running_age=delay(pending),delay(running)
                queued_count=sum(row['count'] for row in pending);running_count=sum(row['count'] for row in running)
                queue.append(c.QueueHealth(kind=kind,queued_count=queued_count,running_count=running_count,
                    oldest_queued_age_seconds=queued_age,oldest_running_age_seconds=running_age))
                counts[kind+'.queued']=queued_count;counts[kind+'.running']=running_count
                oldest.extend(max(0,(now-row['total_oldest']).total_seconds()) for row in rows if row['kind']==kind and row['total_oldest'] is not None)
            active=unit.releases.get_active(mode,bot_scope=scope)
            reasons=[];ready=False
            if not unit.restore_ready():
                reasons.append('restore_pending')
            if not recent:
                reasons.append('worker_missing')
            if active is None or (release_ref is not None and active.ref!=release_ref):
                reasons.append('release_inactive')
            else:
                refs=getattr(c,'runtime_dependency_refs',lambda record:(record.ref,))(active)
                lifecycle=[unit.releases.read_lifecycle(ref) for ref in refs]
                checker=getattr(c,'ensure_active_release_ready',None)
                if checker is not None:
                    decision=checker(active,tuple(item for item in lifecycle if item is not None),mode,now,allow_synthetic_draft=mode=='demo')
                    ready=decision.ok
                    if not ready:
                        reasons.append(str(decision.error.code.value if hasattr(decision.error.code,'value') else decision.error.code).lower())
                else:
                    ready=all(item is not None and not item.revoked and (item.review_due_at is None or now<item.review_due_at) for item in lifecycle)
                    if not ready:
                        reasons.append('release_unavailable')
            metrics=tuple(c.OperationMetric(**row) for row in unit.connection.execute(
                'SELECT name,count,total_seconds,last_seconds,max_seconds,updated_at FROM operation_metrics WHERE bot_scope=%s ORDER BY name',(scope,)).fetchall())
            return c.HealthSnapshot(status='ready' if ready and recent and not reasons else 'not_ready',release_commit=latest['release_commit'] if latest else '',
                db_ready=True,worker_heartbeat_age=age,oldest_job_age=max(oldest) if oldest else None,counts=counts,
                queue_delays=tuple(queue),worker_count=len(recent),release_ready=ready,active_release_ref=active.ref if active else None,reasons=tuple(reasons),operation_metrics=metrics)
    def record_operation_metric(self,bot_scope,name,elapsed_seconds,now):
        import math
        allowed={'queue.process_inbox','queue.render_artifact','queue.deliver_outbox',
                 'compute.process_inbox','compute.render_artifact','compute.deliver_outbox'}
        if name not in allowed or not isinstance(bot_scope,str) or not 1<=len(bot_scope)<=256:
            raise ValueError('Invalid metric name or scope')
        if isinstance(elapsed_seconds,bool) or not isinstance(elapsed_seconds,(int,float)) or not math.isfinite(elapsed_seconds) or elapsed_seconds<0:
            raise ValueError('Invalid metric duration')
        elapsed=min(float(elapsed_seconds),86400.0)
        with self.uow() as unit:
            unit.connection.execute('''INSERT INTO operation_metrics(bot_scope,name,count,total_seconds,last_seconds,max_seconds,updated_at)
                VALUES(%s,%s,1,%s,%s,%s,%s) ON CONFLICT(bot_scope,name) DO UPDATE
                SET count=operation_metrics.count+1,total_seconds=operation_metrics.total_seconds+excluded.last_seconds,
                    last_seconds=excluded.last_seconds,max_seconds=GREATEST(operation_metrics.max_seconds,excluded.last_seconds),updated_at=excluded.updated_at''',
                (bot_scope,name,elapsed,elapsed,elapsed,now))
            unit.commit()

    def active_claims(self,now,bot_scope=None):
        with self.uow() as unit:
            rows=unit.connection.execute("SELECT * FROM jobs WHERE status='running' AND lease_until>%s AND (%s::text IS NULL OR bot_scope=%s) ORDER BY id",(now,bot_scope,bot_scope)).fetchall()
            result=[]
            for row in rows:
                record=unit.work._decode(row)
                if record is not None:
                    result.append(c.ClaimedJob(**record.work.model_dump(),fence_token=record.fence_token,lease_owner=record.lease_owner,lease_until=record.lease_until,
                        attempt=record.attempt,next_attempt_at=record.next_attempt_at,trace_id=record.trace_id,created_at=row.get('created_at')))
            return tuple(result)
    def referenced_blob_refs(self,bot_scope=None):
        with self.uow() as unit:
            rows=unit.connection.execute('SELECT * FROM document_artifacts WHERE octet_length(ciphertext)>0 AND (%s::text IS NULL OR bot_scope=%s)',(bot_scope,bot_scope)).fetchall()
            return tuple(record.encrypted_blob_ref for row in rows if (record:=unit.artifacts._decode(row)) is not None)
    def prepare_retention(self,now,policy,bot_scope=None):
        with self.uow() as unit:
            cutoff=now-timedelta(days=policy.case_days)
            due=unit.connection.execute("""SELECT c.* FROM cases c WHERE c.status<>'deleted' AND c.last_activity_at<=%s
              AND (%s::text IS NULL OR c.bot_scope=%s)
              AND NOT EXISTS(SELECT 1 FROM inbox_events i WHERE i.owner_id=c.owner_id AND (i.case_id IS NULL OR i.case_id=c.id) AND i.status IN ('received','processing'))
              AND NOT EXISTS(SELECT 1 FROM jobs j WHERE (j.case_id=c.id OR (j.case_id IS NULL AND j.owner_id=c.owner_id)) AND j.status='running' AND j.lease_until>%s)
              ORDER BY c.last_activity_at,c.id FOR UPDATE OF c SKIP LOCKED LIMIT %s""",(cutoff,bot_scope,bot_scope,now,policy.batch_size)).fetchall()
            for row in due:
                snapshot=unit.cases._decode(row)
                if snapshot is not None:
                    ctx=c.ActorContext(owner_id=snapshot.owner_id,delivery_target_id=UUID(int=0),bot_scope=row['bot_scope'],case_mode=snapshot.mode,correlation_id=uuid4())
                    unit.cases.mark_deleted(ctx,snapshot.guard,now)
            for table in ('callback_handles','input_candidates','result_previews'):
                unit.connection.execute(sql.SQL("""UPDATE {} SET ciphertext=''::bytea,nonce=''::bytea,tag=''::bytea,active=false WHERE id IN
                  (SELECT t.id FROM {} t WHERE t.expires_at<=%s AND octet_length(t.ciphertext)>0 AND (%s::text IS NULL OR t.bot_scope=%s)
                   AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.case_id=t.case_id AND j.status='running' AND j.lease_until>%s)
                   ORDER BY t.id FOR UPDATE SKIP LOCKED LIMIT %s)""").format(sql.Identifier(table),sql.Identifier(table)),(now,bot_scope,bot_scope,now,policy.batch_size))
            unit.connection.execute("""UPDATE inbox_events SET ciphertext=''::bytea,nonce=''::bytea,tag=''::bytea WHERE id IN
              (SELECT i.id FROM inbox_events i WHERE i.status IN ('processed','ignored','failed') AND i.processed_at<=%s
               AND octet_length(i.ciphertext)>0 AND (%s::text IS NULL OR i.bot_scope=%s)
               AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.inbox_id=i.id AND j.status IN ('queued','retry_wait','running'))
               ORDER BY i.id FOR UPDATE SKIP LOCKED LIMIT %s)""",(now-timedelta(hours=policy.inbox_hours),bot_scope,bot_scope,policy.batch_size))
            artifact_rows=unit.connection.execute("""SELECT a.* FROM document_artifacts a WHERE octet_length(a.ciphertext)>0
              AND (%s::text IS NULL OR a.bot_scope=%s)
              AND (a.status IN ('expired','deleted') OR a.expires_at<=%s OR a.created_at<=%s)
              AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.case_id=a.case_id AND j.status='running' AND j.lease_until>%s)
              ORDER BY a.id FOR UPDATE OF a SKIP LOCKED LIMIT %s""",(bot_scope,bot_scope,now,now-timedelta(days=policy.artifact_days),now,policy.batch_size)).fetchall()
            artifacts=[]
            for row in artifact_rows:
                if row['status']!='deleted':
                    unit.connection.execute("UPDATE document_artifacts SET status='expired',updated_at=%s WHERE id=%s",(now,row['id']))
                    row['status']='expired'
                record=unit.artifacts._decode(row)
                if record is not None:
                    artifacts.append(record)
            # Erased terminal inbox/job/outbox dedupe rows survive at least the policy window.
            old=now-timedelta(days=policy.dedupe_days)
            eligible=unit.connection.execute("""SELECT id FROM jobs WHERE status IN ('succeeded','failed','cancelled') AND updated_at<=%s
              AND (%s::text IS NULL OR bot_scope=%s) AND NOT EXISTS(SELECT 1 FROM outbox_messages o WHERE o.id=jobs.outbox_id AND o.status='sending')
              ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %s""",(old,bot_scope,bot_scope,policy.batch_size)).fetchall()
            ids=[row['id'] for row in eligible]
            if ids:
                unit.connection.execute('DELETE FROM send_attempts WHERE job_id=ANY(%s)',(ids,))
                unit.connection.execute('DELETE FROM jobs WHERE id=ANY(%s)',(ids,))
            unit.connection.execute("DELETE FROM inbox_events WHERE id IN (SELECT i.id FROM inbox_events i WHERE i.processed_at<=%s AND i.status IN ('processed','ignored','failed') AND octet_length(i.ciphertext)=0 AND (%s::text IS NULL OR i.bot_scope=%s) AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.inbox_id=i.id) ORDER BY i.id LIMIT %s)",(old,bot_scope,bot_scope,policy.batch_size))
            unit.connection.execute("DELETE FROM outbox_messages WHERE id IN (SELECT o.id FROM outbox_messages o WHERE o.status IN ('confirmed','definitely_rejected','delivery_unknown') AND o.updated_at<=%s AND (%s::text IS NULL OR o.bot_scope=%s) AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.outbox_id=o.id) AND NOT EXISTS(SELECT 1 FROM send_attempts a WHERE a.outbox_id=o.id) ORDER BY o.id LIMIT %s)",(old,bot_scope,bot_scope,policy.batch_size))
            unit.connection.execute('DELETE FROM quota_counters WHERE expires_at<=%s AND (%s::text IS NULL OR bot_scope=%s)',(now,bot_scope,bot_scope))
            unit.commit()
        cleanup=self.list_cleanup_requests(bot_scope)
        return c.MaintenanceTargets(artifacts=tuple(artifacts),cleanup=cleanup,protected_claims=self.active_claims(now),active_blob_refs=self.referenced_blob_refs())
    def complete_artifact_cleanup(self,artifact_id,blob_ref,now):
        with self.uow() as unit:
            row=unit.artifacts._one('id=%s',(artifact_id,),True)
            record=unit.artifacts._decode(row)
            if record is None:
                return True
            if record.encrypted_blob_ref!=blob_ref or row['status'] not in ('expired','deleted'):
                return False
            if unit.connection.execute("SELECT 1 FROM jobs WHERE case_id=%s AND status='running' AND lease_until>%s LIMIT 1",(record.case_id,now)).fetchone():
                return False
            unit.connection.execute("UPDATE document_artifacts SET ciphertext=''::bytea,nonce=''::bytea,tag=''::bytea WHERE id=%s",(artifact_id,))
            unit.connection.execute("UPDATE uploaded_attachments SET ciphertext=''::bytea,nonce=''::bytea,tag=''::bytea WHERE artifact_id=%s",(artifact_id,))
            unit.connection.execute("UPDATE document_bundles b SET status='expired' WHERE b.manifest_id=%s AND b.status<>'deleted' AND NOT EXISTS(SELECT 1 FROM document_artifacts a WHERE a.manifest_id=b.manifest_id AND a.status='published')",(record.manifest_id,))
            unit.commit()
            return True
    def export_deletion_journal(self,bot_scope=None):
        with self.uow() as unit:
            rows=unit.connection.execute('SELECT * FROM deletion_journal WHERE (%s::text IS NULL OR bot_scope=%s) ORDER BY case_id',(bot_scope,bot_scope)).fetchall()
            return tuple(c.DeletionJournalEntry(**row) for row in rows)
    def apply_deletion_journal(self,entries):
        applied=0
        with self.uow() as unit:
            for entry in sorted(entries,key=lambda item:str(item.case_id)):
                entry=c.DeletionJournalEntry.model_validate(entry)
                stored=unit.connection.execute('SELECT * FROM deletion_journal WHERE case_id=%s FOR UPDATE',(entry.case_id,)).fetchone()
                row=unit.cases._one('id=%s',(entry.case_id,),True)
                if (stored is not None and stored['owner_id']!=entry.owner_id) or (row is not None and row['owner_id']!=entry.owner_id):
                    raise ValueError('ACCESS_DENIED')
                epoch=max(entry.deletion_epoch,stored['deletion_epoch'] if stored else 0,row['deletion_epoch'] if row else 0)
                changed=stored is None or epoch>stored['deletion_epoch'] or (row is not None and (row['status']!='deleted' or row['deletion_epoch']<epoch))
                unit.connection.execute('INSERT INTO deletion_journal(case_id,owner_id,deletion_epoch,deleted_at,bot_scope) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(case_id) DO UPDATE SET deletion_epoch=GREATEST(deletion_journal.deletion_epoch,excluded.deletion_epoch),deleted_at=GREATEST(deletion_journal.deleted_at,excluded.deleted_at)',(entry.case_id,entry.owner_id,epoch,entry.deleted_at,entry.bot_scope))
                if row is not None:
                    unit.connection.execute("UPDATE cases SET status='deleted',deletion_epoch=%s WHERE id=%s",(epoch,entry.case_id))
                    unit.connection.execute('INSERT INTO case_tombstones(case_id,owner_id,deletion_epoch,deleted_at) VALUES(%s,%s,%s,%s) ON CONFLICT(case_id) DO UPDATE SET deletion_epoch=GREATEST(case_tombstones.deletion_epoch,excluded.deletion_epoch)',(entry.case_id,entry.owner_id,epoch,entry.deleted_at))
                    unit.connection.execute("INSERT INTO cleanup_requests(case_id,deletion_epoch) VALUES(%s,%s) ON CONFLICT(case_id) DO UPDATE SET deletion_epoch=GREATEST(cleanup_requests.deletion_epoch,excluded.deletion_epoch),status='pending' WHERE cleanup_requests.deletion_epoch<excluded.deletion_epoch OR %s",(entry.case_id,epoch,changed))
                    unit.connection.execute('UPDATE callback_handles SET active=false WHERE case_id=%s',(entry.case_id,))
                    unit.connection.execute("UPDATE jobs SET status='cancelled',lease_until=NULL WHERE case_id=%s AND status IN ('queued','retry_wait','running') AND NOT(kind='deliver_outbox' AND outbox_id IN (SELECT id FROM outbox_messages WHERE status='sending'))",(entry.case_id,))
                    unit.connection.execute("UPDATE document_artifacts SET status='deleted' WHERE case_id=%s",(entry.case_id,))
                    unit.connection.execute("UPDATE document_bundles SET status='deleted' WHERE case_id=%s",(entry.case_id,))
                applied+=int(changed)
            unit.commit()
        return applied
    def list_backup_artifacts(self):
        with self.uow() as unit:
            rows=unit.connection.execute("SELECT * FROM document_artifacts WHERE status='published' AND octet_length(ciphertext)>0 ORDER BY id").fetchall()
            return tuple(record for row in rows if (record:=unit.artifacts._decode(row)) is not None)
    def set_restore_state(self,state):
        if state not in ('pending','ready','failed'):
            raise ValueError('Invalid restore state')
        with self.uow() as unit:
            unit.connection.execute('CREATE SCHEMA IF NOT EXISTS _tsr_restore_control')
            unit.connection.execute("CREATE TABLE IF NOT EXISTS _tsr_restore_control.restore_gate(id integer PRIMARY KEY CHECK(id=1),state text NOT NULL CHECK(state IN ('pending','ready','failed')))")
            unit.connection.execute('INSERT INTO _tsr_restore_control.restore_gate(id,state) VALUES(1,%s) ON CONFLICT(id) DO UPDATE SET state=excluded.state',(state,))
            unit.commit()
    def restore_ready(self):
        with self.uow() as unit:
            return unit.restore_ready()
    def is_empty_restore_target(self):
        with self._connect() as connection:
            return connection.execute("SELECT NOT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%') AS empty").fetchone()['empty']
    @contextmanager
    def backup_snapshot(self):
        connection=self._connect(backup=True)
        try:
            connection.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
            row=connection.execute('SELECT pg_export_snapshot() AS snapshot_id,current_schema() AS schema_name').fetchone()
            unit=UnitOfWork(connection,self.crypto)
            artifacts=tuple(record for item in connection.execute("SELECT * FROM document_artifacts WHERE status='published' AND octet_length(ciphertext)>0 ORDER BY id").fetchall() if (record:=unit.artifacts._decode(item)) is not None)
            yield c.DatabaseBackupSnapshot(dsn=self.dsn,snapshot_id=row['snapshot_id'],schema_name=row['schema_name'],artifacts=artifacts)
        finally:
            try:
                if not connection.closed:
                    connection.rollback()
            finally:
                connection.close()
    def ping(self):
        try:
            with self._connect() as connection:
                return connection.execute('SELECT 1 AS ready').fetchone()['ready']==1
        except psycopg.Error:
            return False
    @contextmanager
    def uow(self):
        connection=self._connect()
        unit=UnitOfWork(connection,self.crypto)
        try:
            yield unit
        finally:
            # Repositories never commit. Uncommitted writes always roll back.
            try:
                if not connection.closed:
                    connection.rollback()
            finally:
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
        self._private_index_key=None
    def _receipt_digest(self,owner_id,target_id,receipt):
        if self._private_index_key is None:
            row=self.connection.execute('SELECT key_id,nonce,ciphertext,tag FROM private_index_keys WHERE id=1').fetchone()
            if row is None:raise RuntimeError('Private receipt index unavailable')
            blob=c.EncryptedBlob(**{key:bytes(row[key]) if key!='key_id' else row[key] for key in ('key_id','nonce','ciphertext','tag')})
            self._private_index_key=self.crypto.decrypt(blob)
        payload=(str(owner_id)+':'+str(target_id)+':'+type(receipt).__name__+':').encode()+c.canonical_bytes(receipt)
        return hmac.digest(self._private_index_key,payload,'sha256').hex()
    def restore_ready(self):
        present=self.connection.execute("SELECT to_regclass('_tsr_restore_control.restore_gate') AS name").fetchone()['name']
        if present is None:
            return True
        row=self.connection.execute('SELECT state FROM _tsr_restore_control.restore_gate WHERE id=1 FOR SHARE').fetchone()
        return row is not None and row['state']=='ready'
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
        self.ensure_lifecycles(record)
        return saved
    def ensure_lifecycles(self,record):
        dues={record.ref:record.manifest.review.review_due_at}
        for item in record.manifest.files:
            dues.setdefault(item.ref,None)
        for package in record.packages:
            content=package.content
            dues[package.ref]=content.review.review_due_at
            if package.kind=='CatalogPack':
                for offer in content.offers:
                    dues[c.VersionRef(id=str(offer.snapshot_id),version=content.version)]=offer.review.review_due_at
            elif package.kind=='SourcesRegistry':
                for source in content.sources:
                    dues[c.VersionRef(id=source.source_id,version=content.version)]=source.review.review_due_at
            elif package.kind=='TemplateRegistry':
                for template in content.templates:
                    dues[template.ref]=content.review.review_due_at
        for ref in c.runtime_dependency_refs(record):
            dues.setdefault(ref,None)
        for ref,due in sorted(dues.items(),key=lambda item:(item[0].id,item[0].version)):
            changed=self.connection.execute("""INSERT INTO data_lifecycle(id,version,review_due_at) VALUES(%s,%s,%s)
                ON CONFLICT(id,version) DO UPDATE SET review_due_at=CASE WHEN data_lifecycle.review_due_at IS NULL THEN excluded.review_due_at
                  ELSE LEAST(data_lifecycle.review_due_at,excluded.review_due_at) END
                WHERE excluded.review_due_at IS NOT NULL AND (data_lifecycle.review_due_at IS NULL OR excluded.review_due_at<data_lifecycle.review_due_at)
                RETURNING id""",(ref.id,ref.version,due)).fetchone()
            if changed is not None:
                self._audit(ref,'system:package-registration',record.validated_at,'LIFECYCLE_REGISTERED')
        return tuple(self.read_lifecycle(ref) for ref in c.runtime_dependency_refs(record))
    def get(self,ref):
        row=self.connection.execute('SELECT content FROM data_releases WHERE id=%s AND version=%s',(ref.id,ref.version)).fetchone()
        return c.ReleaseRecord.model_validate(row['content']) if row else None
    def read_lifecycle(self,ref,lock=False):
        row=self.connection.execute('SELECT * FROM data_lifecycle WHERE id=%s AND version=%s'+(' FOR UPDATE' if lock else ''),(ref.id,ref.version)).fetchone()
        return c.ReleaseLifecycle(ref=ref,revoked=row['revoked'],review_due_at=row['review_due_at'],reason_code=row['reason_code']) if row else None
    def get_active(self,mode,lock=False,bot_scope='default'):
        row=self.connection.execute('SELECT release_id,release_version FROM active_releases WHERE mode=%s AND bot_scope=%s'+(' FOR UPDATE' if lock else ''),(mode,bot_scope)).fetchone()
        return self.get(c.VersionRef(id=row['release_id'],version=row['release_version'])) if row else None
    def _audit(self,ref,actor_key,now,result):
        self.connection.execute('INSERT INTO source_audit(id,subject_id,actor_key,occurred_at,result_code) VALUES(%s,%s,%s,%s,%s)',(uuid4(),ref.id+':'+ref.version,actor_key,now,result))
    def activate(self,ref,mode,actor_key,now,allow_synthetic_draft=False,only_if_empty=False,bot_scope='default'):
        if mode not in ('demo','pilot'):
            raise ValueError('VALIDATION_ERROR')
        self.connection.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',('tsr-release:'+bot_scope+':'+mode,))
        old=self.get_active(mode,bot_scope=bot_scope)
        if only_if_empty and old is not None:
            return c.ActivationReceipt(previous_ref=old.ref,active_ref=old.ref,activated_at=now,actor_key=actor_key)
        record=self.get(ref)
        if record is None:
            raise ValueError('DATA_NOT_READY')
        lifecycles=tuple(state for dependency in c.runtime_dependency_refs(record)
            if (state:=self.read_lifecycle(dependency,lock=True)) is not None)
        ready=c.ensure_active_release_ready(record,lifecycles,mode,now,allow_synthetic_draft=allow_synthetic_draft)
        if not ready.ok:
            raise ValueError(ready.error.code.value if hasattr(ready.error.code,'value') else ready.error.code)
        self.connection.execute('INSERT INTO active_releases(bot_scope,mode,release_id,release_version) VALUES(%s,%s,%s,%s) ON CONFLICT(bot_scope,mode) DO UPDATE SET release_id=excluded.release_id,release_version=excluded.release_version',(bot_scope,mode,ref.id,ref.version))
        self._audit(ref,actor_key,now,'ACTIVATED')
        return c.ActivationReceipt(previous_ref=old.ref if old else None,active_ref=ref,activated_at=now,actor_key=actor_key)

    def revoke_package(self,ref,reason,actor_key,now):
        return self.revoke(ref,reason,actor_key,now)
    def revoke(self,ref,reason,actor_key,now):
        result=self.connection.execute('UPDATE data_lifecycle SET revoked=true,reason_code=%s WHERE id=%s AND version=%s',(reason,ref.id,ref.version))
        if not result.rowcount:
            raise ValueError('NOT_FOUND')
        self._audit(ref,actor_key,now,'REVOKED')
        return self.read_lifecycle(ref)
