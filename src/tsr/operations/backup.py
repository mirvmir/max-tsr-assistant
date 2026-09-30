"""Authenticated bounded backups and explicit offline restore with deletion replay.

No shell, tar extraction, plaintext dump file or encryption key in an archive.
Source quiescence is an operator attestation, not an implicit dual-write promise.
"""
import base64
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import time
from uuid import uuid4

from tsr.contracts import BackupReceipt, EncryptedBlob, RestoreReport, VerifiedDeletionJournal, content_hash
from tsr.operations.cleanup import cleanup_deleted_cases


def _now(value):
    result=value or datetime.now(timezone.utc)
    if result.tzinfo is None: raise ValueError('UTC timestamp required')
    return result.astimezone(timezone.utc)


def _pg_env(dsn):
    from psycopg.conninfo import conninfo_to_dict
    values=conninfo_to_dict(dsn)
    env={key:os.environ[key] for key in ('PATH','LD_LIBRARY_PATH','LANG') if key in os.environ}
    names={'host':'PGHOST','hostaddr':'PGHOSTADDR','port':'PGPORT','dbname':'PGDATABASE','user':'PGUSER',
           'password':'PGPASSWORD','options':'PGOPTIONS','sslmode':'PGSSLMODE','sslcert':'PGSSLCERT',
           'sslkey':'PGSSLKEY','sslrootcert':'PGSSLROOTCERT','connect_timeout':'PGCONNECT_TIMEOUT'}
    env.update({names[key]:value for key,value in values.items() if key in names})
    env.setdefault('PGCONNECT_TIMEOUT','10')
    return env


def _source_id(dsn,schema):
    from psycopg.conninfo import conninfo_to_dict
    values=conninfo_to_dict(dsn)
    return content_hash({'host':values.get('host',''),'port':values.get('port','5432'),
                         'database':values.get('dbname',''),'schema':schema})


def _pack(plaintext,crypto,magic):
    blob=crypto.encrypt(plaintext)
    envelope={'key_id':blob.key_id, **{key:base64.b64encode(getattr(blob,key)).decode('ascii')
                                     for key in ('nonce','ciphertext','tag')}}
    return magic+json.dumps(envelope,separators=(',',':')).encode('ascii')


def _unpack(path,crypto,magic,limit):
    fd=os.open(Path(path),os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as stream:
        if os.fstat(stream.fileno()).st_size>limit*2+4096: raise ValueError('Backup size limit')
        raw=stream.read(limit*2+4097)
    if not raw.startswith(magic): raise ValueError('Backup format invalid')
    envelope=json.loads(raw[len(magic):])
    blob=EncryptedBlob(key_id=envelope['key_id'],**{key:base64.b64decode(envelope[key],validate=True)
                            for key in ('nonce','ciphertext','tag')})
    plaintext=crypto.decrypt(blob)
    if len(plaintext)>limit: raise ValueError('Backup size limit')
    return plaintext


def _atomic_private(destination,payload,suffix):
    root=Path(destination).resolve();root.mkdir(parents=True,exist_ok=True,mode=0o700);os.chmod(root,0o700)
    ident=uuid4();temporary=root/(ident.hex+'.tmp');final=root/(ident.hex+suffix)
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(payload);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,final)
        directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
        return final
    finally: temporary.unlink(missing_ok=True)


def _dump(snapshot,tool,limit):
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}',snapshot.schema_name): raise ValueError('Unsupported schema')
    args=[str(tool),'--format=custom','--no-owner','--no-privileges','--schema='+snapshot.schema_name,
          '--snapshot='+snapshot.snapshot_id]
    process=subprocess.Popen(args,env=_pg_env(snapshot.dsn),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    data=bytearray();deadline=time.monotonic()+120
    selector=selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ)
    try:
        while True:
            if time.monotonic()>deadline: raise ValueError('Backup database timeout')
            if not selector.select(timeout=1): continue
            chunk=os.read(process.stdout.fileno(),min(65536,limit-len(data)+1))
            if not chunk: break
            data.extend(chunk)
            if len(data)>limit: raise ValueError('Backup database limit')
        if process.wait(timeout=10)!=0 or not data.startswith(b'PGDMP'): raise ValueError('Backup database failed')
        return bytes(data)
    finally:
        selector.close();process.stdout.close()
        if process.poll() is None: process.kill();process.wait()


def purge_backups(destination,policy,now=None):
    cutoff=_now(now)-timedelta(days=policy.retention_days)
    removed=0
    for path in Path(destination).glob('*.tsrb'):
        if re.fullmatch(r'[0-9a-f]{32}\.tsrb',path.name) and not path.is_symlink():
            if datetime.fromtimestamp(path.stat().st_mtime,timezone.utc)<cutoff:
                path.unlink();removed+=1
    return removed


def create_backup(destination,policy,*,db,files,crypto,pg_dump_path='pg_dump',now=None):
    if not policy.maintenance: raise ValueError('Backup requires maintenance gate')
    created=_now(now);backup_id=uuid4()
    try:
        with db.backup_snapshot() as snapshot:
            if len(snapshot.artifacts)>policy.max_blobs: raise ValueError('Backup artifact limit')
            dump=_dump(snapshot,pg_dump_path,policy.max_database_bytes)
            encoded={};entries=[];total=len(dump)*4//3
            for record in snapshot.artifacts:
                if record.size_bytes>policy.max_blob_bytes: raise ValueError('Backup artifact limit')
                exported=files.export_ciphertext(record)
                if not exported.ok: raise ValueError('Backup artifact unavailable')
                payload=exported.value
                if not re.fullmatch(r'[0-9a-f]{32}\.blob',record.encrypted_blob_ref): raise ValueError('Backup artifact path invalid')
                total+=len(payload)*4//3
                if total>policy.max_archive_bytes: raise ValueError('Backup aggregate limit')
                encoded[record.encrypted_blob_ref]=base64.b64encode(payload).decode('ascii')
                entries.append({'ref':record.encrypted_blob_ref,'artifact_id':str(record.artifact_id),
                    'sha256':sha256(payload).hexdigest(),'size_bytes':len(payload),
                    'plaintext_sha256':record.plaintext_sha256,'plaintext_size':record.size_bytes})
            manifest={'schema_version':'1.0.0','backup_id':str(backup_id),'created_at':created.isoformat(),
                'source_id':_source_id(snapshot.dsn,snapshot.schema_name),'schema_name':snapshot.schema_name,
                'database_sha256':sha256(dump).hexdigest(),'database_size':len(dump),'blobs':entries}
            digest=content_hash(manifest)
            plaintext=json.dumps({'manifest':manifest,'manifest_hash':digest,'database':base64.b64encode(dump).decode('ascii'),
                                  'blobs':encoded},ensure_ascii=True,separators=(',',':')).encode('ascii')
            if len(plaintext)>policy.max_archive_bytes: raise ValueError('Backup aggregate limit')
            ciphertext=_pack(plaintext,crypto,b'TSRB1\n')
            location=_atomic_private(destination,ciphertext,'.tsrb')
        purge_backups(destination,policy,created)
        return BackupReceipt(backup_id=backup_id,created_at=created,encrypted_location=str(location),manifest_hash=digest)
    except Exception:
        raise ValueError('Backup failed; no readiness change') from None


def _journal_core(journal):
    return {'entries':[entry.model_dump(mode='json') for entry in journal.entries],
            'complete_through':journal.complete_through.isoformat(),'verified_at':journal.verified_at.isoformat(),
            'source_id':journal.source_id}


def export_deletion_journal(destination,*,db,crypto,now=None,source_offline=False):
    if not source_offline: raise ValueError('Current journal requires source quiescence attestation')
    cutover=_now(now)
    with db.backup_snapshot() as snapshot:
        entries=db.export_deletion_journal()
        provisional=VerifiedDeletionJournal(entries=entries,complete_through=cutover,verified_at=cutover,
            source_id=_source_id(snapshot.dsn,snapshot.schema_name),checksum='pending')
    journal=provisional.model_copy(update={'checksum':content_hash(_journal_core(provisional))})
    # Journal exports have their own private location and are intentionally never
    # removed by the seven-day backup janitor. They must outlive older backups.
    return _atomic_private(destination,_pack(journal.model_dump_json().encode(),crypto,b'TSRJ1\n'),'.tsrj')


def load_deletion_journal(path,*,crypto):
    journal=VerifiedDeletionJournal.model_validate_json(_unpack(path,crypto,b'TSRJ1\n',16*1024*1024))
    if journal.checksum!=content_hash(_journal_core(journal)): raise ValueError('Deletion journal verification failed')
    return journal


def _decode_archive(backup_ref,crypto,policy):
    path=backup_ref.encrypted_location if isinstance(backup_ref,BackupReceipt) else backup_ref
    package=json.loads(_unpack(path,crypto,b'TSRB1\n',policy.max_archive_bytes))
    manifest=package['manifest'];digest=content_hash(manifest)
    if package['manifest_hash']!=digest or (isinstance(backup_ref,BackupReceipt) and backup_ref.manifest_hash!=digest):
        raise ValueError('Backup integrity invalid')
    if manifest['schema_version']!='1.0.0' or not re.fullmatch(r'[a-z][a-z0-9_]{0,62}',manifest['schema_name']):
        raise ValueError('Backup schema invalid')
    dump=base64.b64decode(package['database'],validate=True)
    if len(dump)>policy.max_database_bytes or len(dump)!=manifest['database_size'] or sha256(dump).hexdigest()!=manifest['database_sha256'] or not dump.startswith(b'PGDMP'):
        raise ValueError('Backup database invalid')
    if len(manifest['blobs'])>policy.max_blobs: raise ValueError('Backup artifact limit')
    blobs={}
    for entry in manifest['blobs']:
        ref=entry['ref']
        if not re.fullmatch(r'[0-9a-f]{32}\.blob',ref) or ref in blobs: raise ValueError('Backup artifact path invalid')
        payload=base64.b64decode(package['blobs'][ref],validate=True)
        if (entry['plaintext_size']>policy.max_blob_bytes or len(payload)>policy.max_blob_bytes*2+4096
                or len(payload)!=entry['size_bytes'] or sha256(payload).hexdigest()!=entry['sha256']):
            raise ValueError('Backup artifact integrity invalid')
        blobs[ref]=(entry,payload)
    if set(package['blobs'])!=set(blobs): raise ValueError('Backup artifact set invalid')
    return manifest,dump,blobs


def restore_backup(backup_ref,deletion_log,*,db,files,crypto,policy,pg_restore_path='pg_restore',
                   now=None,required_cutover=None,source_offline=False):
    applied=0;gate_set=False
    def rejected(code):
        return RestoreReport(restored_version='1.0.0',consistency_errors=(code,),deletions_applied=applied,ready=False)
    if not policy.maintenance or not policy.offline or not source_offline: return rejected('restore.offline_gate_required')
    try:
        # Both authentication and complete integrity checks precede ANY DB call.
        manifest,dump,blobs=_decode_archive(backup_ref,crypto,policy)
        if not isinstance(deletion_log,VerifiedDeletionJournal): return rejected('restore.current_journal_required')
        cutoff=_now(required_cutover or now)
        if (deletion_log.checksum!=content_hash(_journal_core(deletion_log))
                or deletion_log.source_id!=manifest['source_id']
                or deletion_log.complete_through<cutoff
                or deletion_log.complete_through<datetime.fromisoformat(manifest['created_at'])):
            return rejected('restore.current_journal_required')
        if policy.restore_target_schema and policy.restore_target_schema!=manifest['schema_name']:
            return rejected('restore.schema_mapping_unsupported')
        for entry,payload in blobs.values():
            if not files.verify_ciphertext(payload,entry['plaintext_sha256'],entry['plaintext_size']).ok:
                return rejected('restore.blob_key_or_integrity_invalid')
        if not db.is_empty_restore_target(): return rejected('restore.target_not_empty')
        db.set_restore_state('pending');gate_set=True
        env=_pg_env(db.dsn)
        # PGDATABASE carries no shell syntax; passwords live only in the child
        # environment. No SQL text/path from a JSON object is executed directly.
        process=subprocess.run([str(pg_restore_path),'--dbname='+env.get('PGDATABASE',''),
            '--no-owner','--no-privileges','--exit-on-error','--single-transaction'],
            input=dump,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
        if process.returncode!=0: raise ValueError('Restore database failed')
        records=db.list_backup_artifacts()
        if {record.encrypted_blob_ref for record in records}!=set(blobs): raise ValueError('Restore artifact set mismatch')
        with db.uow() as unit:
            for record in records:
                frozen=unit.manifests.get(record.manifest_id)
                if (frozen is None or frozen.content.schema_version!='1.0.0'
                        or content_hash(frozen.content)!=frozen.manifest_hash
                        or record.manifest_hash!=frozen.manifest_hash
                        or (record.owner_id,record.case_id,record.case_revision,record.deletion_epoch)
                        !=(frozen.content.owner_id,frozen.content.case_id,frozen.content.case_revision,frozen.content.deletion_epoch)):
                    raise ValueError('Restore manifest binding invalid')
        for record in records:
            entry,payload=blobs[record.encrypted_blob_ref]
            if (str(record.artifact_id)!=entry['artifact_id'] or record.plaintext_sha256!=entry['plaintext_sha256']
                    or record.size_bytes!=entry['plaintext_size']): raise ValueError('Restore artifact metadata mismatch')
            if not files.restore_ciphertext(record.encrypted_blob_ref,payload,record.plaintext_sha256,record.size_bytes).ok:
                raise ValueError('Restore artifact failed')
        applied=db.apply_deletion_journal(deletion_log.entries)
        cleanup=cleanup_deleted_cases(db,files)
        if cleanup.remaining_count or cleanup.errors: raise ValueError('Restore deletion cleanup pending')
        for record in db.list_backup_artifacts():
            if not files.export_ciphertext(record).ok: raise ValueError('Restore final artifact verification failed')
        db.set_restore_state('ready')
        return RestoreReport(restored_version='1.0.0',consistency_errors=(),deletions_applied=applied,ready=True)
    except Exception:
        if gate_set:
            try: db.set_restore_state('failed')
            except Exception: pass
        return rejected('restore.authentication_or_consistency_failed')
