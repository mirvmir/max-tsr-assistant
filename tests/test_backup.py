"""Real pg_dump/pg_restore drill with separate database, blob and archive keys."""
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from test_application import app
from test_cleanup import generated_bundle
from tsr.adapters.crypto import Crypto
from tsr.adapters.db import Database
from tsr.adapters.files import PrivateFiles
from tsr.contracts import BackupPolicy, MaterialPermit
from tsr.operations.backup import (create_backup, export_deletion_journal,
    load_deletion_journal, restore_backup)


def pg_tool(name):
    from importlib.util import find_spec
    return str(Path(find_spec('pgserver').origin).parent / 'pginstall/bin' / name)


@pytest.fixture
def empty_target(app, tmp_path):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    values = conninfo_to_dict(app.db.dsn)
    source_schema = values['options'].split('search_path=')[1]
    base = dict(values)
    base.pop('options', None)
    name = 'restore_' + uuid4().hex
    with psycopg.connect(make_conninfo(**base), autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL('CREATE DATABASE {}').format(psycopg.sql.Identifier(name)))
    target = Database(make_conninfo(**dict(base,dbname=name,options='-c search_path='+source_schema)), Crypto(b'a'*32))
    files = PrivateFiles(tmp_path / 'restored', Crypto(b'c'*32))
    try:
        yield target, files
    finally:
        with psycopg.connect(make_conninfo(**base), autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL('DROP DATABASE {} WITH (FORCE)').format(psycopg.sql.Identifier(name)))


def test_encrypted_round_trip_replays_current_deletion_before_readiness(app, empty_target, tmp_path):
    ctx, case, artifact, files = generated_bundle(app, tmp_path / 'source-blobs')
    retained_ctx, retained_case, retained_artifact, _ = generated_bundle(app, tmp_path / 'source-blobs')
    expected=files.export_ciphertext(retained_artifact).value
    archive_key = Crypto(b'b'*32, key_id='backup-v1')
    policy = BackupPolicy(maintenance=True, offline=True)
    receipt = create_backup(tmp_path / 'backups', policy, db=app.db, files=files,
                            crypto=archive_key, pg_dump_path=pg_tool('pg_dump'))
    archive = Path(receipt.encrypted_location)
    assert archive.exists()
    assert b'PGDMP' not in archive.read_bytes()
    assert 'Вымышленный'.encode() not in archive.read_bytes()
    with app.db.uow() as uow:
        assert uow.cases.mark_deleted(ctx, case.guard, datetime.now(timezone.utc)).ok
        uow.commit()
    cutover = datetime.now(timezone.utc)
    journal_path = export_deletion_journal(tmp_path / 'journal', db=app.db, crypto=archive_key, now=cutover,source_offline=True)
    journal = load_deletion_journal(journal_path, crypto=archive_key)
    db, restored_files = empty_target
    report = restore_backup(receipt, journal, db=db, files=restored_files, crypto=archive_key,
                            policy=policy, pg_restore_path=pg_tool('pg_restore'), now=cutover,source_offline=True,required_cutover=cutover)
    assert report.ready, report.consistency_errors
    assert db.restore_ready()
    assert report.deletions_applied == 1
    with db.uow() as uow:
        assert not uow.cases.get_owned(ctx, case.case_id).ok
        assert uow.artifacts.get(artifact.artifact_id) is None
    assert not (restored_files.root / artifact.encrypted_blob_ref).exists()
    assert db.export_deletion_journal()[0].deletion_epoch == 1
    assert (restored_files.root/retained_artifact.encrypted_blob_ref).read_bytes()==expected
    permit=MaterialPermit(owner_id=retained_ctx.owner_id,case_id=retained_case.case_id,
        current_case_guard=retained_case.guard,artifact_id=retained_artifact.artifact_id,
        artifact_original_revision=retained_case.case_revision,disposition='current',warning_acknowledged=False,
        expires_at=datetime.now(timezone.utc)+timedelta(minutes=1))
    assert restored_files.read_authorized_artifact(permit,retained_artifact).value.startswith(b'%PDF-')
    replay=restore_backup(receipt,journal,db=db,files=restored_files,crypto=archive_key,policy=policy,
        pg_restore_path=pg_tool('pg_restore'),required_cutover=cutover,source_offline=True)
    assert not replay.ready and replay.consistency_errors==('restore.target_not_empty',)


def test_tampered_or_missing_stale_journal_never_touches_empty_database(app, empty_target, tmp_path):
    generated_bundle(app, tmp_path / 'source-blobs')
    key = Crypto(b'b'*32)
    policy = BackupPolicy(maintenance=True, offline=True)
    receipt = create_backup(tmp_path / 'backups', policy, db=app.db, files=PrivateFiles(tmp_path/'source-blobs',Crypto(b'c'*32)),
                            crypto=key,pg_dump_path=pg_tool('pg_dump'))
    cutover = datetime.now(timezone.utc)
    journal = load_deletion_journal(export_deletion_journal(tmp_path/'journal',db=app.db,crypto=key,now=cutover,source_offline=True),crypto=key)
    db, files = empty_target
    missing = restore_backup(receipt,None,db=db,files=files,crypto=key,policy=policy,pg_restore_path=pg_tool('pg_restore'),now=cutover,source_offline=True)
    assert not missing.ready
    assert missing.consistency_errors==('restore.current_journal_required',)
    stale = restore_backup(receipt,journal,db=db,files=files,crypto=key,policy=policy,pg_restore_path=pg_tool('pg_restore'),now=cutover+timedelta(seconds=1),source_offline=True)
    assert not stale.ready
    assert stale.consistency_errors==('restore.current_journal_required',)
    wrong_blob_key=PrivateFiles(tmp_path/'wrong-key',Crypto(b'd'*32))
    wrong=restore_backup(receipt,journal,db=db,files=wrong_blob_key,crypto=key,policy=policy,
        pg_restore_path=pg_tool('pg_restore'),required_cutover=cutover,source_offline=True)
    assert wrong.consistency_errors==('restore.blob_key_or_integrity_invalid',)
    assert db.is_empty_restore_target()
    archive = Path(receipt.encrypted_location)
    corrupt = bytearray(archive.read_bytes()); corrupt[-5] ^= 1; archive.write_bytes(corrupt)
    tampered = restore_backup(receipt,journal,db=db,files=files,crypto=key,policy=policy,pg_restore_path=pg_tool('pg_restore'),now=cutover,source_offline=True)
    assert not tampered.ready
    assert db.is_empty_restore_target()


def test_backup_retention_is_seven_days_and_keeps_deletion_journals(tmp_path):
    from tsr.operations.backup import purge_backups
    now=datetime.now(timezone.utc)
    expired=tmp_path/(uuid4().hex+'.tsrb');expired.write_bytes(b'encrypted fixture')
    current=tmp_path/(uuid4().hex+'.tsrb');current.write_bytes(b'encrypted fixture')
    journal=tmp_path/(uuid4().hex+'.tsrj');journal.write_bytes(b'encrypted journal fixture')
    old=(now-timedelta(days=8)).timestamp()
    os.utime(expired,(old,old));os.utime(journal,(old,old))
    assert purge_backups(tmp_path,BackupPolicy(),now)==1
    assert not expired.exists() and current.exists() and journal.exists()


def test_oversize_database_dump_emits_no_archive(app,tmp_path):
    _,_,_,files=generated_bundle(app,tmp_path/'source')
    with pytest.raises(ValueError,match='Backup failed'):
        create_backup(tmp_path/'backups',BackupPolicy(maintenance=True,max_database_bytes=32),
            db=app.db,files=files,crypto=Crypto(b'b'*32),pg_dump_path=pg_tool('pg_dump'))
    assert not list((tmp_path/'backups').glob('*'))
