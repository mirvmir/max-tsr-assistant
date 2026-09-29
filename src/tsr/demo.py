"""A trusted local MAX dialogue simulator, always using synthetic demo data."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from tsr.contracts import (ActorContext, AttachmentRecord, CallbackReceipt, CaseGuard,
                          CommandEnvelope, IdentityRecord, MessageReceipt, NavigatePayload,
                          Result, StartCasePayload, TransportResult)
from tsr.runtime import TransportBindings
from tsr.worker.dispatcher import Worker


@dataclass(frozen=True)
class DemoReport:
    case_id: UUID
    bundle_id: UUID
    bundle_status: str
    manifest_hash: str
    preview_hash: str
    gap_minor: int | None
    downloads: tuple[Path, ...]
    messages: tuple[str, ...]


class LocalTransport:
    """Only local receipts; this class never calls MAX or claims MAX delivery."""
    def __init__(self, bindings, download_dir=None):
        self.bindings = bindings
        self.download_dir = Path(download_dir) if download_dir is not None else None
        self.messages = []
        self.downloads = []
        self.sequence = 0

    def _receipt(self):
        self.sequence += 1
        return TransportResult(status="confirmed", receipt=MessageReceipt(
            operation="send_message", message_id=f"local-demo-{self.sequence}", accepted_at=datetime.now(timezone.utc)))

    def send_view(self, permit, payload):
        self.bindings.recipient(permit)
        labels = [button.text for row in payload.buttons for button in row]
        displayed = payload.text
        if labels:
            displayed += "\nКнопки: " + " · ".join(labels)
        self.messages.append(displayed)
        return self._receipt()

    def answer_callback(self, permit, answer):
        self.bindings.recipient(permit)
        return TransportResult(status="confirmed", receipt=CallbackReceipt(
            callback_id=answer.platform_callback_id, acknowledged_at=datetime.now(timezone.utc)))

    def upload_file(self, permit, data, mime_type, filename):
        reference = uuid4()
        record = AttachmentRecord(attachment_token_ref=reference, artifact_id=permit.artifact_id,
                                  owner_id=permit.owner_id, manifest_hash=permit.manifest_hash,
                                  state="ready", observed_at=datetime.now(timezone.utc), token="local-demo-" + reference.hex)
        self.bindings.write_attachment(record)
        # Only synthetic bytes are read; the local upload is an in-memory simulation.
        return Result.success(record)

    def send_material(self, permit, reference):
        self.bindings.recipient(permit)
        attachment = self.bindings.attachment(permit, reference)
        with self.bindings.db.uow() as uow:
            artifact = uow.artifacts.get(attachment.material_permit.artifact_id)
        content = self.bindings.files.read_authorized_artifact(attachment.material_permit, artifact)
        if not content.ok:
            return TransportResult(status="definitely_rejected", error_code="local_material_unavailable")
        if self.download_dir is not None:
            self.download_dir.mkdir(parents=True, exist_ok=True)
            path = self.download_dir / f"{artifact.artifact_id.hex}.{artifact.format}"
            path.write_bytes(content.value)
            self.downloads.append(path)
        return self._receipt()


def create_demo_actor(settings, db):
    if settings.mode != "demo":
        raise ValueError("local_simulator_requires_demo")
    owner, target = uuid4(), uuid4()
    identity = IdentityRecord(identity_id=uuid4(), owner_id=owner, delivery_target_id=target,
                              bot_scope=settings.bot_scope, lookup_key="local-" + owner.hex,
                              platform_user_id="1", platform_chat_id="1")
    with db.uow() as uow:
        uow.identities.insert(identity)
        uow.commit()
    return ActorContext(owner_id=owner, delivery_target_id=target, bot_scope=settings.bot_scope,
                        case_mode="demo", correlation_id=uuid4())


def _checked(result):
    if not result.ok:
        raise ValueError(f"Demo failed safely: {result.error.code}")
    return result.value


def run_demo_scenario(settings, db, application, files, branch="purchase", download_dir=None, role="self"):
    if branch not in ("purchase", "support"):
        raise ValueError("unknown_demo_branch")
    from tsr.application import Application

    # The emulator's queue is separate from the running bot worker's scope.
    settings = settings.model_copy(update={"bot_scope": "local-demo-" + uuid4().hex})
    application = Application(db, application.release, settings, application.clock, application.ids)
    ctx = create_demo_actor(settings, db)
    transport = LocalTransport(TransportBindings(db, files), download_dir)
    worker = Worker(settings, db, application, files, transport)

    def execute(envelope):
        outcome = _checked(application.execute_command(envelope))
        worker.drain()
        return outcome

    def choose(outcome, key):
        selected = next(a for a in outcome.view.actions if a.label_key == key)
        result = _checked(application.execute_action(ctx, selected.action_handle))
        worker.drain()
        return result

    def text_and_confirm(outcome, value):
        proposed = _checked(application.execute_text(ctx, value, outcome.case.case_id))
        worker.drain()
        return choose(proposed, "confirm")

    try:
        result = execute(CommandEnvelope(command_id=uuid4(), actor=ctx, type="start_case",
                                         payload=StartCasePayload(category_ref=application.release.profile.ref, requested_role=role)))
        for answer in ("420", "100", "да", "100000", "да"):
            result = text_and_confirm(result, answer)
        compared = choose(result, "compare")
        # Comparison actions are ordered together with the displayed offer cards.
        selected = _checked(application.execute_action(ctx, compared.view.actions[0].action_handle))
        worker.drain()
        result = choose(selected, branch)
        if branch == "support":
            for answer in ("ru-alt", "да", "Демонстрационный Заявитель", "Синтетический адрес"):
                result = text_and_confirm(result, answer)
        prepare = next(a for a in result.view.actions if a.label_key == "prepare")
        with db.uow() as uow:
            handle = _checked(uow.handles.resolve(ctx, prepare.action_handle, datetime.now(timezone.utc)))
            preview = uow.previews.get(handle.action.typed_payload.preview_id)
        confirmed = _checked(application.execute_action(ctx, prepare.action_handle))
        worker.drain()
        current = execute(CommandEnvelope(command_id=uuid4(), actor=ctx,
                        case_guard=CaseGuard(case_id=confirmed.case.case_id, expected_revision=confirmed.case.case_revision,
                                             expected_deletion_epoch=confirmed.case.deletion_epoch),
                        type="navigate", payload=NavigatePayload(destination="materials")))
        with db.uow() as uow:
            download_actions = tuple(a for a in current.view.actions
                                     if _checked(uow.handles.resolve(ctx, a.action_handle, datetime.now(timezone.utc))).action.command_type == "request_material")
        for action in download_actions:
            _checked(application.execute_action(ctx, action.action_handle))
            worker.drain()
        with db.uow() as uow:
            bundle = uow.bundles.get(confirmed.created_bundle_id)
            manifest = uow.manifests.get(bundle.manifest_id)
        return DemoReport(current.case.case_id, bundle.bundle_id, bundle.status, manifest.manifest_hash,
                          preview.manifest_hash, manifest.content.pricing.gap.minor if manifest.content.pricing.gap else None,
                          tuple(transport.downloads), tuple(transport.messages))
    finally:
        worker.close()
