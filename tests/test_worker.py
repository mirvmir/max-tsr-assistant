from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import time

import pytest
from pydantic import SecretStr

from tsr.config import Settings, load_settings
from tsr.contracts import ClaimedJob, DomainError, RenderedBytes, Result, TransportResult
from tsr.worker.dispatcher import ActiveRender, Worker
from tsr.worker.recovery import classify_retry


def _claim(kind='render_artifact', attempt=1):
    now = datetime.now(timezone.utc)
    return ClaimedJob(job_id=uuid4(), kind=kind, payload_ref=uuid4(), owner_id=uuid4(),
                      dedupe_key=uuid4().hex, fence_token=1, lease_owner='test',
                      lease_until=now + timedelta(seconds=60), attempt=attempt,
                      next_attempt_at=now, trace_id=uuid4())


def _settings(**updates):
    return Settings(database_url='postgres://postgres@localhost/postgres',
                    encryption_key=SecretStr('11' * 32), identity_hmac_key=SecretStr('test'),
                    **updates)


def _hang_render(marker):
    Path(marker).write_text('started')
    time.sleep(120)


def _child_value():
    return 7


def _stop_test_pool(pool):
    for process in list((getattr(pool, '_processes', None) or {}).values()):
        if process.is_alive():
            process.kill()
        process.join(timeout=1)
    pool.shutdown(wait=False, cancel_futures=True)


def test_ambiguous_send_has_no_automatic_retry():
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    decision = classify_retry('deliver_outbox', TransportResult(status='unknown', reason_code='timeout'), now, 1)
    assert decision.mode == 'explicit_user'
    assert decision.next_attempt_at is None


def test_definite_attachment_processing_failure_is_bounded_safe_retry():
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    outcome = TransportResult(status='definitely_rejected', error_code='attachment.not.ready', retryable=True, retry_after=8)
    decision = classify_retry('deliver_outbox', outcome, now, 1)
    assert decision.mode == 'safe'
    assert decision.next_attempt_at >= now + timedelta(seconds=8)
    assert classify_retry('deliver_outbox', outcome, now, 5).mode == 'none'


@pytest.mark.parametrize('status,expected', [('pending', 'safe'), ('preparing', 'safe'),
                                            ('retry_wait', 'safe'), ('sending', 'none'),
                                            ('delivery_unknown', 'none'), (None, 'none')])
def test_upload_retry_requires_durable_pre_send_state(status, expected):
    error = DomainError(code='TEMPORARY_FAILURE', retryability='safe', safe_message_key='upload.failed')
    now = datetime.now(timezone.utc)
    assert classify_retry('deliver_outbox', error, now, 1, outbox_status=status).mode == expected
    assert classify_retry('deliver_outbox', error, now, 5, outbox_status=status).mode == 'none'


def test_retry_attempts_and_render_timeout_are_bounded_configuration():
    assert _settings().render_timeout_seconds == 45
    assert _settings(render_timeout_seconds=0.25).render_timeout_seconds == 0.25
    with pytest.raises(ValueError):
        _settings(render_timeout_seconds=0)
    with pytest.raises(ValueError):
        _settings(render_timeout_seconds=float('inf'))
    with pytest.raises(ValueError):
        _settings(max_attempts=21)
    configured = load_settings({'TSR_DATABASE_URL': 'postgres://postgres@localhost/postgres',
                                'TSR_ENCRYPTION_KEY': '11' * 32, 'TSR_IDENTITY_HMAC_KEY': 'test',
                                'TSR_RENDER_TIMEOUT_SECONDS': '2.5', 'TSR_MAX_CA_BUNDLE': '/certs/local-ca.pem'})
    assert configured.ok and configured.value.render_timeout_seconds == 2.5
    assert configured.value.max_ca_bundle == Path('/certs/local-ca.pem')


def test_real_stuck_child_times_out_and_render_slot_is_reusable(tmp_path):
    worker = Worker(_settings(), None, None, None, None)
    failures = []
    worker._failure = lambda claim, error: failures.append((claim, error))
    marker = tmp_path / 'child-started'
    pool = worker.render_pool
    try:
        future = pool.submit(_hang_render, str(marker))
        processes = list(pool._processes.values())
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), 'spawned render child did not start'
        worker.render = ActiveRender(_claim(), None, future, time.monotonic() - 46)
        worker._poll()
        assert worker.render is None
        assert len(failures) == 1
        assert all(not process.is_alive() for process in processes)
        assert worker.render_pool is not pool
        assert worker.render_pool._mp_context.get_start_method() == 'spawn'
        assert worker.render_pool.submit(_child_value).result(timeout=10) == 7
    finally:
        _stop_test_pool(pool)
        _stop_test_pool(worker.render_pool)
        worker.delivery_pool.shutdown(wait=True)


def test_close_terminates_stuck_render_child_within_bound(tmp_path):
    worker = Worker(_settings(), None, None, None, None)
    worker._failure = lambda *args: None
    marker = tmp_path / 'child-started'
    pool = worker.render_pool
    try:
        future = pool.submit(_hang_render, str(marker))
        processes = list(pool._processes.values())
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        worker.render = ActiveRender(_claim(), None, future, time.monotonic())
        start = time.monotonic()
        worker.close()
        assert time.monotonic() - start < 3
        assert all(not process.is_alive() for process in processes)
        assert worker.render is None
    finally:
        _stop_test_pool(pool)
        worker.delivery_pool.shutdown(wait=False)


@pytest.mark.parametrize('field', ['job_id', 'fence_token', 'manifest_hash', 'format'])
def test_uncorrelated_render_result_cannot_stage_or_publish(field):
    worker = Worker(_settings(), None, None, None, None)
    claim = _claim()
    context = SimpleNamespace(manifest=SimpleNamespace(manifest_hash='expected-hash'),
                              payload=SimpleNamespace(artifact_spec=SimpleNamespace(format='pdf')))
    fields = dict(job_id=claim.job_id, fence_token=claim.fence_token, manifest_hash='expected-hash',
                  format='pdf', mime_type='application/pdf', plaintext_sha256='hash', bytes=b'%PDF-test')
    fields[field] = {'job_id': uuid4(), 'fence_token': 2, 'manifest_hash': 'wrong-hash', 'format': 'docx'}[field]
    future = Future()
    future.set_result(Result.success(RenderedBytes(**fields)))
    worker.render = ActiveRender(claim, context, future, time.monotonic())
    failures, staged, published = [], [], []
    worker._failure = lambda claim, error: failures.append(error)
    worker.files = SimpleNamespace(stage_encrypted=lambda *args: staged.append(args))
    worker.application = SimpleNamespace(publish_render_result=lambda *args: published.append(args))
    try:
        worker._poll()
        assert len(failures) == 1
        assert failures[0].retryability == 'none'
        assert not staged
        assert not published
    finally:
        worker.close()


from test_db import db


@pytest.mark.parametrize('status', ['pending', 'preparing', 'retry_wait', 'sending', 'delivery_unknown'])
def test_worker_retries_upload_only_under_current_fence_and_durable_state(db, status):
    from tsr.application import Application
    from tsr.contracts import DeliveryIntent, OutboxRecord, SendPermit, WorkRef, content_hash
    from tsr.operations.releases import load_demo_release

    settings = _settings()
    application = Application(db, load_demo_release(Path(__file__).parents[1]), settings)
    owner, target, outbox_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    intent = DeliveryIntent(outbox_id=outbox_id, owner_id=owner, delivery_target_id=target,
                            kind='view', view_ref=uuid4(), dedupe_key=uuid4().hex, created_at=now)
    work = WorkRef(job_id=uuid4(), kind='deliver_outbox', payload_ref=outbox_id,
                   owner_id=owner, dedupe_key=uuid4().hex)
    with db.uow() as uow:
        uow.outbox.append_unique(OutboxRecord(outbox_id=outbox_id, owner_id=owner, intent=intent,
                                             status='pending' if status == 'sending' else status))
        uow.work.enqueue_unique(work)
        uow.commit()
    now = datetime.now(timezone.utc)
    with db.uow() as uow:
        claim = uow.work.claim_next('deliver_outbox', 'test', now, 60)
        if status == 'sending':
            permit = SendPermit(outbox_id=outbox_id, send_attempt_id=uuid4(), job_id=claim.job_id,
                                fence_token=claim.fence_token, owner_id=owner, delivery_target_id=target,
                                approved_at=now, payload_hash=content_hash(None), expires_at=now + timedelta(seconds=60))
            assert uow.outbox.begin_send_if_allowed(claim, permit, now).ok
        uow.commit()
    worker = Worker(settings, db, application, None, None)
    error = DomainError(code='TEMPORARY_FAILURE', retryability='safe', safe_message_key='upload.failed')
    try:
        # A stale child/thread must not change the current attempt or state.
        worker._failure(claim.model_copy(update={'fence_token': claim.fence_token + 1}), error)
        with db.uow() as uow:
            assert uow.work.get(claim.job_id).status == 'running'
        worker._failure(claim, error)
        with db.uow() as uow:
            job = uow.work.get(claim.job_id)
            assert job.status == ('retry_wait' if status in ('pending', 'preparing', 'retry_wait') else 'failed')
            assert uow.outbox.get(outbox_id).status == status
            if job.status == 'failed':
                assert uow.work.claim_next('deliver_outbox', 'next', now + timedelta(seconds=120), 60) is None
            uow.commit()
    finally:
        worker.close()


def test_pre_send_material_upload_failure_does_not_authorize_send(db):
    from tsr.contracts import DeliveryIntent, OutboxRecord, WorkRef

    owner, target, outbox_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    intent = DeliveryIntent(outbox_id=outbox_id, owner_id=owner, delivery_target_id=target,
                            kind='view', view_ref=uuid4(), dedupe_key=uuid4().hex, created_at=now)
    work = WorkRef(job_id=uuid4(), kind='deliver_outbox', payload_ref=outbox_id,
                   owner_id=owner, dedupe_key=uuid4().hex)
    with db.uow() as uow:
        uow.outbox.append_unique(OutboxRecord(outbox_id=outbox_id, owner_id=owner, intent=intent))
        uow.work.enqueue_unique(work)
        uow.commit()
    with db.uow() as uow:
        claim = uow.work.claim_next('deliver_outbox', 'test', datetime.now(timezone.utc), 60)
        uow.commit()
    error = DomainError(code='TEMPORARY_FAILURE', retryability='safe', safe_message_key='upload.failed')
    send_authorizations = []
    context = SimpleNamespace(artifact=SimpleNamespace(owner_id=owner, artifact_id=uuid4(),
                                                       manifest_hash='hash', format='pdf'),
                              permit=object(), upload_permit=object())
    application = SimpleNamespace(
        get_delivery_payload=lambda claim: Result.success(SimpleNamespace(intent=SimpleNamespace(kind='material'))),
        get_material_context=lambda claim: Result.success(context),
        authorize_delivery=lambda claim: send_authorizations.append(claim))
    files = SimpleNamespace(read_authorized_artifact=lambda *args: Result.success(b'%PDF-test'))
    transport = SimpleNamespace(upload_file=lambda *args: Result(ok=False, error=error))
    worker = Worker(_settings(), db, application, files, transport)
    try:
        result = worker._deliver(claim)
        assert not result.ok and result.error == error
        assert not send_authorizations
        worker._failure(claim, result.error)
        with db.uow() as uow:
            assert uow.work.get(claim.job_id).status == 'retry_wait'
            assert uow.outbox.get(outbox_id).status == 'pending'
    finally:
        worker.close()


def test_local_view_format_failure_precedes_durable_sending(monkeypatch):
    import tsr.adapters.max

    claim = _claim('deliver_outbox')
    send_authorizations, sends = [], []
    application = SimpleNamespace(
        get_delivery_payload=lambda claim: Result.success(SimpleNamespace(intent=SimpleNamespace(kind='view'), payload=object())),
        authorize_delivery=lambda claim: send_authorizations.append(claim))
    transport = SimpleNamespace(send_view=lambda *args: sends.append(args))
    worker = Worker(_settings(), None, application, None, transport)

    def invalid_view(payload):
        raise ValueError('local formatting failed')

    monkeypatch.setattr(tsr.adapters.max, 'render_max_view', invalid_view)
    try:
        result = worker._deliver(claim)
        assert not result.ok
        assert result.error.code == 'PERMANENT_FAILURE'
        assert result.error.retryability == 'none'
        assert not send_authorizations
        assert not sends
    finally:
        worker.close()


def test_worker_recovery_routes_configured_limit_and_scope_to_application():
    from tsr.contracts import RecoveryReport

    calls = []
    report = RecoveryReport()
    application = SimpleNamespace(recover_work=lambda **kwargs: (calls.append(kwargs), Result.success(report))[1])
    worker = Worker(_settings(max_attempts=2, bot_scope='only-this-bot'), None, application, None, None)
    now = datetime.now(timezone.utc)
    worker._now = lambda: now
    try:
        assert worker.recover() == report
        assert calls == [dict(now=now, max_attempts=2, bot_scope='only-this-bot')]
    finally:
        worker.close()


def test_crashed_renderer_exhaustion_through_worker_is_terminal_and_fenced(db):
    from test_application import actor, prepare_flow
    from tsr.application import Application
    from tsr.contracts import IdentityRecord, StagedArtifact
    from tsr.operations.releases import load_demo_release

    settings = _settings(max_attempts=2)
    application = Application(db, load_demo_release(Path(__file__).parents[1]), settings)
    ctx = actor()
    with db.uow() as uow:
        uow.identities.insert(IdentityRecord(identity_id=uuid4(), owner_id=ctx.owner_id,
                                             delivery_target_id=ctx.delivery_target_id,
                                             bot_scope=settings.bot_scope, lookup_key=uuid4().hex,
                                             platform_chat_id='synthetic-worker-exhaustion'))
        uow.commit()
    case, manifest = prepare_flow(application, ctx, 'purchase')
    worker = Worker(settings, db, application, None, None)
    now = datetime.now(timezone.utc)
    worker._now = lambda: now
    old_claim, old_staged = None, None
    try:
        for attempt in (1, 2):
            with db.uow() as uow:
                claim = uow.work.claim_next('render_artifact', 'crashed-renderer', now, 1,
                                            bot_scope=settings.bot_scope)
                assert claim is not None and claim.attempt == attempt
                uow.commit()
            if old_claim is None:
                context = application.get_render_context(claim, now)
                assert context.ok, context.error
                spec = context.value.payload.artifact_spec
                old_claim = claim
                old_staged = StagedArtifact(artifact_id=uuid4(), job_id=claim.job_id,
                                           fence_token=claim.fence_token, manifest_id=manifest.manifest_id,
                                           document_kind=spec.document_kind, format=spec.format,
                                           plaintext_sha256='a' * 64, encrypted_blob_ref='private-test/stale',
                                           size_bytes=10, created_at=now)
            now += timedelta(seconds=2)
            report = worker.recover()
            if attempt == 1:
                assert claim.job_id in report.requeued_ids
            else:
                assert claim.job_id in report.exhausted_ids
        with db.uow() as uow:
            assert uow.work.get(claim.job_id).status == 'failed'
            assert uow.work.claim_next('render_artifact', 'replacement', now + timedelta(days=1), 60,
                                       bot_scope=settings.bot_scope) is None
            assert uow.bundles.find_by_manifest(manifest.manifest_id).status == 'failed'
            notices = [record for record in uow.outbox.list_by_case(case.case_id)
                       if record.intent.dedupe_key.startswith('bundle-failed:')]
            assert len(notices) == 1
            uow.commit()
        stale = application.publish_render_result(old_claim, old_staged)
        assert not stale.ok and stale.error.code == 'LEASE_LOST'
        with db.uow() as uow:
            assert not uow.artifacts.list_by_case(case.case_id)
    finally:
        worker.close()


def test_idle_worker_heartbeat_is_persistent_and_scoped(db):
    worker = Worker(_settings(bot_scope='operational-worker'),db,None,None,None,worker_id='worker-operational')
    now = datetime.now(timezone.utc)
    worker._now = lambda:now
    try:
        worker._heartbeat()
        health = db.collect_health(now,'operational-worker')
        assert health.worker_count == 1 and health.worker_heartbeat_age == 0
        assert db.collect_health(now,'other-bot').worker_count == 0
    finally:
        worker.close()


def test_scheduled_maintenance_is_single_and_does_not_block_heartbeat(db,tmp_path):
    import os
    import threading
    from tsr.adapters.files import PrivateFiles
    settings=_settings(bot_scope='maintenance-worker',backup_root=tmp_path/'backups',private_root=tmp_path/'blobs')
    settings.backup_root.mkdir()
    old=settings.backup_root/(uuid4().hex+'.tsrb')
    journal=settings.backup_root/'current.tsrj'
    old.write_bytes(b'old synthetic encrypted backup')
    journal.write_bytes(b'independent synthetic current deletion journal')
    stale=time.time()-8*86400
    os.utime(old,(stale,stale));os.utime(journal,(stale,stale))
    files=PrivateFiles(settings.private_root,db.crypto)
    worker=Worker(settings,db,None,files,None)
    release=threading.Event()
    entered=threading.Event()
    real_maintenance=worker._maintenance
    def delayed_maintenance():
        entered.set()
        assert release.wait(3)
        return real_maintenance()
    worker._maintenance=delayed_maintenance
    try:
        worker._poll_maintenance(settings.maintenance_interval_seconds+1)
        assert entered.wait(1)
        first=worker.maintenance_future
        worker._poll_maintenance(settings.maintenance_interval_seconds*2+2)
        assert worker.maintenance_future is first
        worker._heartbeat()
        assert db.collect_health(datetime.now(timezone.utc),settings.bot_scope).worker_count==1
        release.set()
        assert not first.result(timeout=3).errors
        assert db.read_subscription_health(settings.bot_scope).status=='missing'
        assert not old.exists() and journal.exists()
    finally:
        release.set()
        worker.close()


def test_worker_passes_exact_registry_to_child_and_child_rejects_foreign_renderer():
    from test_documents import frozen_manifest,renamed_registry
    from tsr.contracts import RenderPayload,RenderJobContext,content_hash
    from tsr.domain.documents import build_document_request
    from tsr.worker.dispatcher import _render_in_child
    frozen=frozen_manifest('purchase')
    registry=renamed_registry()
    entry=registry.templates[0]
    content=frozen.content.model_copy(update={'template_refs':(entry.ref,)})
    manifest=frozen.model_copy(update={'content':content,'manifest_hash':content_hash(content)})
    spec=build_document_request(manifest,registry=registry).required_artifacts[0]
    context=RenderJobContext(manifest=manifest,template_registry=registry,
        payload=RenderPayload(manifest_id=manifest.manifest_id,bundle_id=uuid4(),artifact_spec=spec))
    captured=[]
    class CapturePool:
        def submit(self,function,request):
            captured.append(request)
            result=Future();result.set_result(function(request));return result
        def shutdown(self,**kwargs): pass
    worker=Worker(_settings(),None,SimpleNamespace(get_render_context=lambda _:Result.success(context)),None,None)
    worker.render_pool.shutdown(wait=False,cancel_futures=True)
    worker.render_pool=CapturePool()
    try:
        worker._start_render(_claim())
        assert captured[0].template_registry==registry
        assert worker.render.future.result().ok
        altered=entry.model_copy(update={'assets':(entry.assets[0].model_copy(update={'renderer_id':'untrusted-executable'}),)})
        bad=registry.model_copy(update={'templates':(altered,)})
        assert not _render_in_child(captured[0].model_copy(update={'template_registry':bad})).ok
    finally:
        worker.render=None
        worker.close()


def test_safe_json_worker_alert_exposes_trace_and_code_without_exception_text():
    import json
    import logging
    from tsr.worker.dispatcher import SafeJSONFormatter
    trace,job,correlation=uuid4(),uuid4(),uuid4()
    record=logging.LogRecord('tsr.worker',logging.WARNING,__file__,1,'work_outcome',(),
        (RuntimeError,RuntimeError('postgres://private-secret@host/personal-content'),None))
    record.job_id=str(job);record.trace_id=str(trace);record.correlation_id=correlation;record.code='TEMPORARY_FAILURE'
    rendered=SafeJSONFormatter().format(record)
    assert json.loads(rendered)=={'event':'work_outcome','level':'warning','job_id':str(job),
                                 'trace_id':str(trace),'correlation_id':str(correlation),'code':'TEMPORARY_FAILURE'}
    assert 'private-secret' not in rendered and 'personal-content' not in rendered


def test_production_operation_metrics_are_atomic_scoped_and_bounded(db):
    import math
    now=datetime.now(timezone.utc)
    db.record_operation_metric('metrics-own','compute.render_artifact',0.2,now)
    db.record_operation_metric('metrics-own','compute.render_artifact',0.4,now)
    db.record_operation_metric('metrics-foreign','compute.render_artifact',0.8,now)
    with db.uow() as unit:
        row=unit.connection.execute('SELECT * FROM operation_metrics WHERE bot_scope=%s',('metrics-own',)).fetchone()
        assert row['count']==2 and row['total_seconds']==pytest.approx(0.6)
        assert row['last_seconds']==0.4 and row['max_seconds']==0.4
        assert 'owner_id' not in row and 'job_id' not in row and 'trace_id' not in row
    for bad in (-1,math.nan,math.inf):
        with pytest.raises(ValueError): db.record_operation_metric('metrics-own','compute.render_artifact',bad,now)
    with pytest.raises(ValueError): db.record_operation_metric('metrics-own','personal-input',1,now)
    metrics=db.collect_health(now,'metrics-own').operation_metrics
    assert len(metrics)==1 and metrics[0].name=='compute.render_artifact' and metrics[0].count==2
    assert metrics[0].max_seconds==0.4


def test_worker_records_queue_delay_from_trusted_claim_metadata(db):
    from tsr.adapters.max import parse_update,persist_update
    settings=_settings(bot_scope='fixture')
    now=datetime.now(timezone.utc)
    body=(Path(__file__).parent/'fixtures/max/text.json').read_bytes()
    assert persist_update(db,settings,parse_update(body),now)
    worker=Worker(settings,db,None,None,None)
    try:
        claim=worker._claim('process_inbox')
        assert claim is not None and claim.created_at is not None
        metrics=db.collect_health(datetime.now(timezone.utc),settings.bot_scope).operation_metrics
        assert len(metrics)==1 and metrics[0].name=='queue.process_inbox' and metrics[0].count==1
        assert metrics[0].last_seconds>=0
    finally:
        worker.close()
