"""Real published ciphertext cleanup; failures preserve the tombstone request."""
from datetime import datetime, timezone
import pytest

from test_application import app, actor, prepare_flow
from tsr.adapters.crypto import Crypto
from tsr.adapters.files import PrivateFiles
from tsr.adapters.render import render_document
from tsr.contracts import RenderRequest, Result
from tsr.domain.documents import build_document_model
from tsr.operations.cleanup import cleanup_deleted_cases


def generated_bundle(app, tmp_path):
    ctx = actor()
    case, manifest = prepare_flow(app, ctx, 'purchase')
    files = PrivateFiles(tmp_path, Crypto(b'c' * 32))
    with app.db.uow() as uow:
        claim = uow.work.claim_next('render_artifact', 'cleanup-test', datetime.now(timezone.utc), 60)
        uow.commit()
    context = app.get_render_context(claim).value
    spec = context.payload.artifact_spec
    model = build_document_model(manifest, spec.document_kind).value
    rendered = render_document(RenderRequest(job_id=claim.job_id, fence_token=claim.fence_token,
        manifest_hash=manifest.manifest_hash, document_kind=spec.document_kind, format=spec.format,
        template_ref=spec.template_ref, model=model, max_output_bytes=2_000_000))
    assert rendered.ok, rendered.error
    staged = files.stage_encrypted(rendered.value, claim, context.payload)
    assert staged.ok, staged.error
    published = app.publish_render_result(claim, staged.value)
    assert published.ok, published.error
    with app.db.uow() as uow:
        case = uow.cases.get(case.case_id)
    return ctx, case, published.value, files


def test_generated_blob_cleanup_owner_epoch_and_idempotence(app, tmp_path):
    ctx, case, artifact, files = generated_bundle(app, tmp_path)
    blob = tmp_path / artifact.encrypted_blob_ref
    assert blob.exists()
    with app.db.uow() as uow:
        assert not uow.cases.mark_deleted(actor(), case.guard, datetime.now(timezone.utc)).ok
        uow.commit()
    assert not app.db.list_cleanup_requests()
    assert blob.exists()
    with app.db.uow() as uow:
        assert uow.cases.mark_deleted(ctx, case.guard, datetime.now(timezone.utc)).ok
        uow.commit()
    assert not app.db.complete_case_cleanup(case.case_id, case.deletion_epoch)
    with app.db.uow() as uow:
        assert uow.artifacts.get(artifact.artifact_id) is not None
        assert not uow.artifacts.get_owned(ctx, artifact.artifact_id).ok
    report = cleanup_deleted_cases(app.db, files)
    assert report.removed_count == 1 and report.remaining_count == 0 and not report.errors
    assert not blob.exists()
    with app.db.uow() as uow:
        row = uow.connection.execute('SELECT ciphertext FROM document_artifacts WHERE id=%s', (artifact.artifact_id,)).fetchone()
        assert bytes(row['ciphertext']) == b''
        assert uow.artifacts.get(artifact.artifact_id) is None
    replay = cleanup_deleted_cases(app.db, files)
    assert replay.removed_count == 0 and replay.remaining_count == 0 and not replay.errors
    assert files.delete_blob(artifact.encrypted_blob_ref).ok


@pytest.mark.parametrize('failed_step', ['file', 'metadata'])
def test_cleanup_failure_does_not_clear_artifact_metadata(app, tmp_path, monkeypatch, failed_step):
    ctx, case, artifact, files = generated_bundle(app, tmp_path)
    with app.db.uow() as uow:
        assert uow.cases.mark_deleted(ctx, case.guard, datetime.now(timezone.utc)).ok
        uow.commit()

    class UnavailableFiles:
        def delete_blob(self, ref):
            return Result.failure('TEMPORARY_FAILURE', safe_message_key='file_delete_failed')

    original = app.db.complete_case_cleanup
    def unavailable_metadata(*args):
        raise OSError('Test cleanup storage unavailable')
    if failed_step == 'metadata':
        monkeypatch.setattr(app.db, 'complete_case_cleanup', unavailable_metadata)
    failed = cleanup_deleted_cases(app.db, UnavailableFiles() if failed_step == 'file' else files)
    assert failed.remaining_count == 1 and failed.errors
    assert (tmp_path / artifact.encrypted_blob_ref).exists() == (failed_step == 'file')
    with app.db.uow() as uow:
        assert uow.artifacts.get(artifact.artifact_id) is not None
    monkeypatch.setattr(app.db, 'complete_case_cleanup', original)
    recovered = cleanup_deleted_cases(app.db, files)
    assert recovered.remaining_count == 0 and not recovered.errors
