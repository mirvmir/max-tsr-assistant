from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from tsr.adapters.crypto import Crypto
from tsr.adapters.files import PrivateFiles
from tsr.contracts import (ArtifactRecord, ArtifactSpec, CaseGuard, ClaimedJob,
                          MaterialPermit, RenderPayload, RenderedBytes, VersionRef)


def test_aes_gcm_round_trip_random_nonce_and_tamper_rejection():
    crypto = Crypto(b'x' * 32)
    first = crypto.encrypt('Иван Иванов'.encode())
    second = crypto.encrypt('Иван Иванов'.encode())
    assert first.nonce != second.nonce
    assert crypto.decrypt(first) == 'Иван Иванов'.encode()
    assert first.ciphertext != 'Иван Иванов'.encode()
    corrupt = first.model_copy(update={'tag': bytes([first.tag[0] ^ 1]) + first.tag[1:]})
    with pytest.raises(ValueError):
        crypto.decrypt(corrupt)


def test_private_storage_blocks_external_and_symlink_paths(tmp_path):
    storage = PrivateFiles(tmp_path / 'private', Crypto(b'x' * 32))
    outside = tmp_path / 'external'
    outside.write_bytes(b'protected')
    alias = uuid4().hex + '.blob'
    (storage.root / alias).symlink_to(outside)
    assert storage.remove_blob('../external') is False
    assert storage.remove_blob(str(outside)) is False
    assert storage.remove_blob(alias) is False
    assert outside.read_bytes() == b'protected'


def staged_fixture(storage):
    from hashlib import sha256
    now = datetime.now(timezone.utc)
    owner, case, job = uuid4(), uuid4(), uuid4()
    data = 'Персональные сведения'.encode()
    claim = ClaimedJob(job_id=job, kind='render_artifact', payload_ref=uuid4(),
                       owner_id=owner, case_id=case, case_revision=3, deletion_epoch=0,
                       dedupe_key='test', fence_token=2, lease_owner='test',
                       lease_until=now + timedelta(minutes=1), attempt=1,
                       next_attempt_at=now, trace_id=uuid4())
    payload = RenderPayload(manifest_id=uuid4(), bundle_id=uuid4(),
                           artifact_spec=ArtifactSpec(document_kind='purchase_card', format='pdf',
                           template_ref=VersionRef(id='demo-purchase-card', version='1.0.0')))
    rendered = RenderedBytes(job_id=job, fence_token=2, manifest_hash='a' * 64,
                            format='pdf', mime_type='application/pdf',
                            plaintext_sha256=sha256(data).hexdigest(), bytes=data)
    staged = storage.stage_encrypted(rendered, claim, payload)
    assert staged.ok, staged.error
    record = ArtifactRecord(**staged.value.model_dump(), owner_id=owner, case_id=case,
                            case_revision=3, deletion_epoch=0, published_at=now, manifest_hash=rendered.manifest_hash,
                            expires_at=now + timedelta(days=1))
    permit = MaterialPermit(owner_id=owner, case_id=case, artifact_id=record.artifact_id,
                            current_case_guard=CaseGuard(case_id=case, expected_revision=3,
                                                        expected_deletion_epoch=0),
                            artifact_original_revision=3, disposition='current',
                            warning_acknowledged=False, expires_at=now+timedelta(minutes=2))
    return rendered, claim, payload, record, permit


def test_ciphertext_only_authorized_read_fencing_history_and_ttl(tmp_path):
    storage = PrivateFiles(tmp_path, Crypto(b'x' * 32))
    rendered, claim, payload, record, permit = staged_fixture(storage)
    ciphertext = (tmp_path / record.encrypted_blob_ref).read_bytes()
    assert rendered.bytes not in ciphertext
    assert storage.read_authorized_artifact(permit, record).value == rendered.bytes
    assert not storage.read_authorized_artifact(permit.model_copy(update={'owner_id':uuid4()}),record).ok
    stale = rendered.model_copy(update={'fence_token':1})
    assert not storage.stage_encrypted(stale,claim,payload).ok
    guard = permit.current_case_guard.model_copy(update={'expected_revision':4})
    historical = permit.model_copy(update={'current_case_guard':guard,'disposition':'historical'})
    assert not storage.read_authorized_artifact(historical,record).ok
    acknowledged = historical.model_copy(update={'warning_acknowledged':True})
    assert storage.read_authorized_artifact(acknowledged,record).ok
    expired = record.model_copy(update={'expires_at':datetime.now(timezone.utc)-timedelta(seconds=1)})
    assert not storage.read_authorized_artifact(permit,expired).ok
    orphan_tmp = tmp_path / (uuid4().hex + '.tmp')
    orphan_tmp.write_bytes(ciphertext)
    assert storage.purge_orphans([record.encrypted_blob_ref], datetime.now(timezone.utc)+timedelta(seconds=1)) == 1
    assert storage.purge_orphans([record.encrypted_blob_ref], datetime.now(timezone.utc)+timedelta(seconds=1)) == 0
    assert storage.purge_orphans([], datetime.now(timezone.utc)+timedelta(seconds=1)) == 1
