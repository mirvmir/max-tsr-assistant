"""Composition of owner-bound transport resolvers. No secrets read at import."""
from datetime import datetime, timezone

from tsr.contracts import MaterialPermit


class TransportBindings:
    def __init__(self, db, files):
        self.db, self.files = db, files

    def _outbox(self, uow, permit):
        if not uow.restore_ready():
            raise ValueError("restore_not_ready")
        record = uow.outbox.get(permit.outbox_id)
        if (record is None or record.status != "sending" or record.send_permit != permit
                or record.owner_id != permit.owner_id or permit.expires_at <= datetime.now(timezone.utc)):
            raise ValueError("send_not_authorized")
        if permit.case_guard:
            case = uow.cases.get(permit.case_guard.case_id)
            if (case is None or case.owner_id != permit.owner_id or case.status in ("deleted", "deleting")
                    or case.case_revision != permit.case_guard.expected_revision
                    or case.deletion_epoch != permit.case_guard.expected_deletion_epoch):
                raise ValueError("send_not_authorized")
        return record

    def recipient(self, permit):
        with self.db.uow() as uow:
            self._outbox(uow, permit)
            identity = uow.identities.find_by_target(permit.owner_id, permit.delivery_target_id)
            if identity is None:
                raise ValueError("recipient_not_authorized")
            return identity

    def attachment(self, permit, reference):
        from tsr.adapters.max import AuthorizedAttachment

        with self.db.uow() as uow:
            outbox = self._outbox(uow, permit)
            material = outbox.payload
            record = uow.attachments.get(reference)
            if not isinstance(material, MaterialPermit) or record is None:
                raise ValueError("attachment_not_authorized")
            artifact = uow.artifacts.get(material.artifact_id)
            if (artifact is None or artifact.owner_id != permit.owner_id
                    or artifact.artifact_id != record.artifact_id or record.owner_id != permit.owner_id
                    or artifact.expires_at <= datetime.now(timezone.utc)):
                raise ValueError("attachment_not_authorized")
            return AuthorizedAttachment(record, material, artifact.manifest_hash)

    def write_attachment(self, record):
        with self.db.uow() as uow:
            if not uow.restore_ready():
                raise ValueError("restore_not_ready")
            artifact = uow.artifacts.get(record.artifact_id)
            case = uow.cases.get(artifact.case_id) if artifact else None
            if (artifact is None or case is None or case.status in ("deleted", "deleting")
                    or artifact.owner_id != record.owner_id or artifact.manifest_hash != record.manifest_hash
                    or artifact.expires_at <= datetime.now(timezone.utc)):
                raise ValueError("attachment_not_authorized")
            uow.attachments.insert(record)
            uow.commit()
        return record.attachment_token_ref

    def message(self, permit, receipt):
        with self.db.uow() as uow:
            self._outbox(uow, permit)
            return uow.outbox.receipt_exists(permit.owner_id, permit.delivery_target_id, receipt)


def live_transport(settings, bindings):
    import httpx
    import ssl
    from tsr.adapters.max import MaxTransport, DatabaseRequestGate

    if settings.max_token is None or not settings.max_token.get_secret_value():
        raise ValueError("max_token_not_configured")
    # Optional operator-supplied trusted CA bundle; certificate/hostname verification stays on.
    context = ssl.create_default_context(cafile=str(settings.max_ca_bundle) if settings.max_ca_bundle else None)
    client = httpx.Client(timeout=settings.network_timeout_seconds, follow_redirects=False,
                          trust_env=False, verify=context)
    return MaxTransport(client, settings.max_token.get_secret_value(), bindings.recipient,
                        attachment_resolver=bindings.attachment, attachment_writer=bindings.write_attachment,
                        message_authorizer=bindings.message, base_url=settings.max_api_url,
                        timeout=settings.network_timeout_seconds, max_upload_bytes=settings.max_file_bytes,
                        request_gate=DatabaseRequestGate(bindings.db,settings.bot_scope))
