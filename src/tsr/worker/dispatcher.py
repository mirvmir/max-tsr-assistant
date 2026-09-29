"""Short inbox work continues while one renderer and one sender are active."""
from __future__ import annotations

from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import multiprocessing
import signal
import time
from uuid import uuid4

from tsr.contracts import ActorContext, ClaimedJob, DomainError, RenderJobContext, RenderRequest, Result, TransportResult
from tsr.domain.documents import build_document_model
from tsr.worker.recovery import classify_retry

logger = logging.getLogger("tsr.worker")


def _render_in_child(request: RenderRequest):
    # The child entrypoint imports only DTOs, pure document content and renderer.
    from tsr.adapters.render import render_document

    return render_document(request)


@dataclass
class ActiveRender:
    claim: ClaimedJob
    context: RenderJobContext
    future: Future
    started: float


@dataclass
class ActiveDelivery:
    claim: ClaimedJob
    future: Future


class Worker:
    def __init__(self, settings, db, application, files, transport, *, worker_id=None):
        self.settings, self.db, self.application = settings, db, application
        self.files, self.transport = files, transport
        self.worker_id = worker_id or "worker-" + uuid4().hex
        self.render_pool = self._new_render_pool()
        self.delivery_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tsr-send")
        self.render: ActiveRender | None = None
        self.delivery: ActiveDelivery | None = None
        self.stopping = False
        self.last_heartbeat = 0.0
        self.last_recovery = 0.0

    def _new_render_pool(self):
        return ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))

    def _stop_render_pool(self, *, replace=True):
        pool = self.render_pool
        # Python 3.12/3.13 have no terminate_workers API. Capture processes before
        # shutdown clears them; cancellation alone cannot stop a running render.
        processes = tuple((getattr(pool, "_processes", None) or {}).values())
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(timeout=0.5)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.5)
        pool.shutdown(wait=False, cancel_futures=True)
        if replace and not self.stopping:
            self.render_pool = self._new_render_pool()

    def _now(self):
        return datetime.now(timezone.utc)

    def _claim(self, kind):
        with self.db.uow() as uow:
            claim = uow.work.claim_next(kind, self.worker_id, self._now(), self.settings.lease_seconds,
                                        bot_scope=self.settings.bot_scope)
            uow.commit()
            return claim

    def _error(self, code="TEMPORARY_FAILURE", safe=True):
        return DomainError(code=code, retryability="safe" if safe else "none",
                           safe_message_key="worker.operation_failed")

    def _failure(self, claim, error):
        now = self._now()
        if claim.kind == "deliver_outbox":
            # The durable job fence and outbox state are checked under the same
            # locks as begin_send. A pre-send upload failure cannot be inferred
            # safe once this attempt has entered sending or lost its lease.
            with self.db.uow() as uow:
                if not uow.work.is_claim_current(claim, now):
                    return
                record = uow.outbox.get(claim.payload_ref)
                if record is None or record.owner_id != claim.owner_id:
                    return
                ctx = ActorContext(owner_id=claim.owner_id,
                                   delivery_target_id=record.intent.delivery_target_id,
                                   bot_scope=self.settings.bot_scope, case_mode=self.settings.mode,
                                   correlation_id=claim.trace_id)
                locked = uow.outbox.get_owned(ctx, claim.payload_ref, lock=True)
                status = locked.value.status if locked.ok else None
                decision = classify_retry(claim.kind, error, now, claim.attempt,
                                          self.settings.max_attempts, outbox_status=status)
                if decision.mode == "safe":
                    uow.work.retry_safe(claim, now, decision.next_attempt_at)
                    uow.commit()
                    return
        else:
            decision = classify_retry(claim.kind, error, now, claim.attempt, self.settings.max_attempts)
        if decision.mode == "safe":
            with self.db.uow() as uow:
                uow.work.retry_safe(claim, now, decision.next_attempt_at)
                uow.commit()
        else:
            self.application.fail_work(claim, error, now)
        logger.warning("work_outcome", extra={"job_id": str(claim.job_id), "code": str(error.code)})

    def _start_render(self, claim):
        checked = self.application.get_render_context(claim)
        if not checked.ok:
            self._failure(claim, checked.error)
            return
        context = checked.value
        spec = context.payload.artifact_spec
        model = build_document_model(context.manifest, spec.document_kind)
        if not model.ok:
            self._failure(claim, model.error)
            return
        request = RenderRequest(job_id=claim.job_id, fence_token=claim.fence_token,
                                manifest_hash=context.manifest.manifest_hash,
                                document_kind=spec.document_kind, format=spec.format,
                                template_ref=spec.template_ref, model=model.value,
                                max_output_bytes=self.settings.max_file_bytes)
        self.render = ActiveRender(claim, context, self.render_pool.submit(_render_in_child, request), time.monotonic())

    def _upload_ref(self, claim, context):
        with self.db.uow() as uow:
            existing = uow.attachments.find_for_artifact(context.artifact.owner_id,
                                                       context.artifact.artifact_id,
                                                       context.artifact.manifest_hash)
        if existing is not None:
            return Result.success(existing.attachment_token_ref)
        plaintext = self.files.read_authorized_artifact(context.permit, context.artifact)
        if not plaintext.ok:
            return plaintext
        fmt = context.artifact.format
        mime = "application/pdf" if fmt == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        uploaded = self.transport.upload_file(context.upload_permit, plaintext.value, mime,
                                              f"{context.artifact.artifact_id.hex}.{fmt}")
        if not uploaded.ok:
            return uploaded
        return Result.success(uploaded.value.attachment_token_ref)

    def _deliver(self, claim):
        record = self.application.get_delivery_payload(claim)
        if not record.ok:
            return record
        message = None
        if record.value.intent.kind == "view":
            # Local formatting cannot have reached MAX. Complete it before the
            # durable sending boundary so its failure is never ambiguous.
            from tsr.adapters.max import render_max_view

            try:
                message = render_max_view(record.value.payload)
            except Exception:
                return Result(ok=False, error=self._error("PERMANENT_FAILURE", safe=False))
        attachment_ref = None
        if record.value.intent.kind == "material":
            context = self.application.get_material_context(claim)
            if not context.ok:
                return context
            uploaded = self._upload_ref(claim, context.value)
            if not uploaded.ok:
                return uploaded
            attachment_ref = uploaded.value
        permit = self.application.authorize_delivery(claim)
        if not permit.ok:
            return permit
        # A durable SendPermit/sending exists before any message HTTP call.
        try:
            if record.value.intent.kind == "view":
                outcome = self.transport.send_view(permit.value, message)
            elif record.value.intent.kind == "callback_answer":
                outcome = self.transport.answer_callback(permit.value, record.value.payload)
            else:
                outcome = self.transport.send_material(permit.value, attachment_ref)
        except Exception:
            # Once sending begins, even an unexpected exception is ambiguous.
            outcome = TransportResult(status="unknown", reason_code="transport_exception")
        if outcome.status == "definitely_rejected" and outcome.retryable and claim.attempt >= self.settings.max_attempts:
            outcome = outcome.model_copy(update={"retryable": False})
        return self.application.record_delivery_result(claim, outcome)

    def _poll(self):
        if self.render is not None and time.monotonic() - self.render.started >= self.settings.render_timeout_seconds:
            active, self.render = self.render, None
            self._stop_render_pool()
            self._failure(active.claim, self._error())
        if self.render is not None and self.render.future.done():
            active, self.render = self.render, None
            try:
                result = active.future.result()
                if not result.ok:
                    self._stop_render_pool()
                    self._failure(active.claim, result.error)
                else:
                    rendered = result.value
                    if (rendered.job_id != active.claim.job_id
                            or rendered.fence_token != active.claim.fence_token
                            or rendered.manifest_hash != active.context.manifest.manifest_hash
                            or rendered.format != active.context.payload.artifact_spec.format):
                        self._stop_render_pool()
                        self._failure(active.claim, self._error("PERMANENT_FAILURE", safe=False))
                    else:
                        staged = self.files.stage_encrypted(result.value, active.claim, active.context.payload)
                        if not staged.ok:
                            self._failure(active.claim, staged.error)
                        else:
                            published = self.application.publish_render_result(active.claim, staged.value)
                            if not published.ok:
                                self.files.remove_blob(staged.value.encrypted_blob_ref)
                                self._failure(active.claim, published.error)
            except Exception:
                self._stop_render_pool()
                self._failure(active.claim, self._error())
        if self.delivery is not None and self.delivery.future.done():
            active, self.delivery = self.delivery, None
            try:
                result = active.future.result()
                if not result.ok:
                    self._failure(active.claim, result.error)
            except Exception:
                # Recovery preserves sending as unknown if the delivery thread
                # escaped before its own result commit; it never resends it.
                logger.error("delivery_handler_failed", extra={"job_id": str(active.claim.job_id)})

    def _heartbeat(self):
        now = self._now()
        for active in (self.render, self.delivery):
            if active is None:
                continue
            with self.db.uow() as uow:
                renewed = uow.work.renew_lease(active.claim, now, self.settings.lease_seconds)
                uow.commit()
            if renewed:
                active.claim = active.claim.model_copy(update={"lease_until": now + timedelta(seconds=self.settings.lease_seconds)})
        record_heartbeat = getattr(self.db, "record_worker_heartbeat", None)
        if record_heartbeat:
            record_heartbeat(self.worker_id, now, self.settings.release_commit)

    def recover(self):
        # Application owns the single semantic commit: terminal recovery and
        # the current bundle's failure notice must become durable together.
        result = self.application.recover_work(now=self._now(), max_attempts=self.settings.max_attempts,
                                               bot_scope=self.settings.bot_scope)
        if not result.ok:
            raise RuntimeError("worker_recovery_failed")
        return result.value

    def tick(self) -> int:
        self._poll()
        current = time.monotonic()
        if current - self.last_heartbeat >= self.settings.heartbeat_seconds:
            self._heartbeat()
            self.last_heartbeat = current
        if current - self.last_recovery >= self.settings.lease_seconds:
            self.recover()
            self.last_recovery = current
        if self.stopping:
            return 0
        started = 0
        for _ in range(4):
            claim = self._claim("process_inbox")
            if claim is None:
                break
            try:
                result = self.application.handle_inbox(claim)
                if not result.ok:
                    self._failure(claim, result.error)
            except Exception:
                self._failure(claim, self._error())
            started += 1
        # Capacity is checked BEFORE acquiring the rendering lease.
        if self.render is None:
            claim = self._claim("render_artifact")
            if claim:
                self._start_render(claim)
                started += 1
        if self.delivery is None:
            claim = self._claim("deliver_outbox")
            if claim:
                self.delivery = ActiveDelivery(claim, self.delivery_pool.submit(self._deliver, claim))
                started += 1
        return started

    def drain(self, timeout=60):
        """Used by deterministic local smoke checks; never called by HTTP."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            started = self.tick()
            if not started and self.render is None and self.delivery is None:
                return
            time.sleep(0.025)
        raise TimeoutError("Worker did not drain within the smoke deadline")

    def close(self):
        self.stopping = True
        # Shutdown stops rendering immediately; its unfinished fenced job is
        # recovered by the next worker rather than leaving a live child behind.
        self.render = None
        self._stop_render_pool(replace=False)
        deadline = time.monotonic() + min(self.settings.network_timeout_seconds + 5, 30)
        while self.delivery is not None and time.monotonic() < deadline:
            self._poll()
            time.sleep(0.05)
        self.delivery_pool.shutdown(wait=self.delivery is None, cancel_futures=True)

    def run(self):
        def stop(_signal, _frame):
            self.stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            while not self.stopping:
                try:
                    self.tick()
                except Exception:
                    logger.error("worker_iteration_failed")
                time.sleep(0.1)
        finally:
            self.close()
