"""Explicit, owner-bound MAX transport. The caller durably records sending first."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import threading
import time
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
from tsr.contracts import (AttachmentTokenRecord, CallbackAnswer, CallbackReceipt, IdentityRecord,
                          MaterialPermit, MessageReceipt, Result, SendPermit, TransportResult,
                          UploadPermit, UploadResult, content_hash)
from .presenter import MaxMessagePayload


@dataclass(frozen=True)
class AuthorizedAttachment:
    record: AttachmentTokenRecord
    material_permit: MaterialPermit
    manifest_hash: str


@dataclass(frozen=True)
class SubscriptionHealth:
    healthy: bool
    reason_code: str
    secret_verified: bool = False


class RequestGate:
    """Conservative in-process admission; worker count must respect the shared bot budget."""
    def __init__(self, monotonic=time.monotonic):
        self._clock = monotonic
        self._global = deque()
        self._recipient = {}
        self._lock = threading.Lock()

    def admit(self, recipient: UUID | None = None) -> float | None:
        with self._lock:
            now = self._clock()
            while self._global and self._global[0] <= now - 1:
                self._global.popleft()
            if len(self._global) >= 30:
                return max(0.001, self._global[0] + 1 - now)
            # Avoid indefinite retention of owner IDs in a long-lived process.
            self._recipient = {key: value for key, value in self._recipient.items() if value > now - 1}
            if recipient in self._recipient and now - self._recipient[recipient] < 0.5:
                return 0.5 - (now - self._recipient[recipient])
            self._global.append(now)
            if recipient is not None:
                self._recipient[recipient] = now
        return None


class MaxTransport:
    def __init__(self, client: httpx.Client, token: str,
                 recipient_resolver: Callable[[SendPermit], IdentityRecord], *,
                 attachment_resolver: Callable[[SendPermit, UUID], AuthorizedAttachment] | None = None,
                 attachment_writer: Callable[[AttachmentTokenRecord], UUID] | None = None,
                 message_authorizer: Callable[[SendPermit, MessageReceipt], bool] | None = None,
                 base_url: str = 'https://platform-api2.max.ru', clock=None,
                 timeout: float = 10.0, request_gate: RequestGate | None = None,
                 max_upload_bytes: int = 10 * 1024 * 1024,
                 upload_hosts: tuple[str, ...] = ('fu.oneme.ru', 'omu.okcdn.ru', 'omub.okcdn.ru', 'iu.oneme.ru')):
        parsed = urlparse(base_url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('invalid_max_base_url')
        if not token or not isinstance(client, httpx.Client) or not callable(recipient_resolver):
            raise ValueError('explicit_max_dependencies_required')
        if timeout <= 0 or max_upload_bytes <= 0:
            raise ValueError('invalid_max_limits')
        self._client, self._token = client, token
        self._resolve_recipient = recipient_resolver
        self._resolve_attachment, self._write_attachment = attachment_resolver, attachment_writer
        self._authorize_message = message_authorizer
        self._base = base_url.rstrip('/')
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._timeout = timeout
        self._gate = request_gate or RequestGate()
        self._max_upload_bytes = max_upload_bytes
        self._upload_hosts = frozenset(upload_hosts)

    @staticmethod
    def _rejected(code: str, retryable=False, retry_after=None) -> TransportResult:
        return TransportResult(status='definitely_rejected', error_code=code, retryable=retryable, retry_after=retry_after)

    def _recipient(self, permit: SendPermit) -> IdentityRecord:
        now = self._clock()
        if not isinstance(permit, SendPermit) or not permit.approved_at <= now < permit.expires_at:
            raise ValueError('send_permit_expired')
        identity = self._resolve_recipient(permit)
        if not isinstance(identity, IdentityRecord) or (identity.owner_id, identity.delivery_target_id) != (permit.owner_id, permit.delivery_target_id):
            raise ValueError('recipient_owner_mismatch')
        if not identity.platform_user_id or not re.fullmatch(r'[1-9][0-9]{0,18}', identity.platform_user_id):
            raise ValueError('private_recipient_missing')
        return identity

    def _request(self, method, path, *, params=None, body=None):
        try:
            response = self._client.request(method, self._base + path, params=params, json=body,
                         headers={'Authorization': self._token}, timeout=self._timeout, follow_redirects=False)
        except httpx.RequestError:
            return None, None, TransportResult(status='unknown', reason_code='network_outcome_unknown')
        try:
            data = response.json() if len(response.content) <= 2 * 1024 * 1024 else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            data = None
        code = data.get('code') if data else None
        # Only safe machine codes are stored; no server exception/message text.
        code = code if isinstance(code, str) and re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}', code) else None
        if code == 'attachment.not.ready':
            return response, data, self._rejected(code, True, 1.0)
        if 400 <= response.status_code < 500 and response.status_code != 408:
            retryable = response.status_code == 429
            retry_after = None
            if retryable:
                try:
                    retry_after = max(0.0, min(float(response.headers.get('Retry-After', '1')), 3600))
                except ValueError:
                    retry_after = 1.0
            return response, data, self._rejected(code or 'http_rejected', retryable, retry_after)
        if not 200 <= response.status_code < 300:
            return response, data, TransportResult(status='unknown', reason_code='http_outcome_unknown')
        if data is None:
            return response, data, TransportResult(status='unknown', reason_code='invalid_response')
        if data.get('success') is False or code:
            return response, data, self._rejected(code or 'platform_rejected')
        return response, data, None

    def _send(self, permit: SendPermit, body: dict, source_hash: str) -> TransportResult:
        if not isinstance(permit, SendPermit):
            return self._rejected('invalid_send_permit')
        if permit.payload_hash != source_hash:
            return self._rejected('payload_binding_mismatch')
        try:
            identity = self._recipient(permit)
        except (ValueError, LookupError, PermissionError):
            return self._rejected('recipient_not_authorized')
        retry = self._gate.admit(permit.delivery_target_id)
        if retry is not None:
            return self._rejected('local_rate_limit', True, retry)
        _, data, failure = self._request('POST', '/messages', params={'user_id':identity.platform_user_id,'disable_link_preview':'true'}, body=body)
        if failure:
            return failure
        message = data.get('message')
        content = message.get('body') if isinstance(message, dict) else None
        mid = content.get('mid') if isinstance(content, dict) else None
        if not isinstance(mid, str) or not 1 <= len(mid) <= 512:
            return TransportResult(status='unknown', reason_code='message_receipt_missing')
        return TransportResult(status='confirmed', receipt=MessageReceipt(operation='send_message', message_id=mid, accepted_at=self._clock()))

    def send_view(self, permit: SendPermit, payload: MaxMessagePayload) -> TransportResult:
        if not isinstance(payload, MaxMessagePayload):
            return self._rejected('invalid_view_payload')
        return self._send(permit, payload.as_api_dict(), payload.source_hash)

    def send_material(self, permit: SendPermit, attachment_ref: UUID) -> TransportResult:
        if not self._resolve_attachment:
            return self._rejected('attachment_not_authorized')
        try:
            bound = self._resolve_attachment(permit, attachment_ref)
            record, material = bound.record, bound.material_permit
            if not isinstance(bound, AuthorizedAttachment) or not isinstance(material, MaterialPermit) or not isinstance(record, AttachmentTokenRecord):
                raise ValueError('invalid_attachment')
            if (record.owner_id, material.owner_id, record.artifact_id, record.attachment_token_ref) != (permit.owner_id, permit.owner_id, material.artifact_id, attachment_ref):
                raise ValueError('attachment_owner_mismatch')
            if record.manifest_hash != bound.manifest_hash or material.current_case_guard != permit.case_guard or self._clock() >= material.expires_at:
                raise ValueError('material_not_current')
            if material.disposition == 'historical' and not material.warning_acknowledged:
                raise ValueError('historical_warning_missing')
            if record.state == 'unknown' or not record.token:
                raise ValueError('attachment_unavailable')
            body = {'attachments':[{'type':'file','payload':{'token':record.token}}]}
            if material.disposition == 'historical':
                body['text'] = 'Исторический материал: сведения могли измениться. Файл относится к прежнему результату.'
            return self._send(permit, body, content_hash(material))
        except (ValueError, LookupError, PermissionError, AttributeError):
            return self._rejected('attachment_not_authorized')

    def edit_known_message(self, permit: SendPermit, receipt: MessageReceipt, payload: MaxMessagePayload) -> TransportResult:
        try:
            self._recipient(permit)
            if not isinstance(receipt, MessageReceipt) or not self._authorize_message or not self._authorize_message(permit, receipt):
                raise ValueError('message_not_owned')
            if permit.payload_hash != payload.source_hash:
                raise ValueError('payload_binding_mismatch')
        except (ValueError, LookupError, PermissionError):
            return self._rejected('message_not_authorized')
        retry = self._gate.admit(permit.delivery_target_id)
        if retry is not None:
            return self._rejected('local_rate_limit', True, retry)
        _, data, failure = self._request('PUT','/messages',params={'message_id':receipt.message_id},body=payload.as_api_dict())
        if failure:
            return failure
        if data.get('success') is not True:
            return TransportResult(status='unknown', reason_code='edit_receipt_missing')
        return TransportResult(status='confirmed', receipt=MessageReceipt(operation='edit_message',message_id=receipt.message_id,accepted_at=self._clock()))

    def answer_callback(self, permit: SendPermit, answer: CallbackAnswer) -> TransportResult:
        from tsr.application.presenter import resolve_message
        try:
            self._recipient(permit)
            if not isinstance(answer, CallbackAnswer) or answer.owner_id != permit.owner_id or content_hash(answer) != permit.payload_hash:
                raise ValueError('callback_binding_mismatch')
        except (ValueError, LookupError, PermissionError):
            return self._rejected('callback_not_authorized')
        retry = self._gate.admit(permit.delivery_target_id)
        if retry is not None:
            return self._rejected('local_rate_limit', True, retry)
        _, data, failure = self._request('POST','/answers',params={'callback_id':answer.platform_callback_id},
                                        body={'notification':resolve_message(answer.text_key,answer.parameters)[:256]})
        if failure:
            return failure
        if data.get('success') is not True:
            return TransportResult(status='unknown',reason_code='callback_receipt_missing')
        return TransportResult(status='confirmed',receipt=CallbackReceipt(callback_id=answer.platform_callback_id,acknowledged_at=self._clock()))

    def upload_file(self, permit: UploadPermit, data: bytes, mime_type: str, filename: str) -> Result[UploadResult]:
        if not isinstance(permit, UploadPermit) or not permit.approved_at <= self._clock() < permit.expires_at or not self._write_attachment:
            return Result.failure('ACCESS_DENIED')
        if not isinstance(data, bytes) or not 0 < len(data) <= self._max_upload_bytes:
            return Result.failure('VALIDATION_ERROR')
        if mime_type not in {'application/pdf','application/vnd.openxmlformats-officedocument.wordprocessingml.document'} or filename not in {str(permit.artifact_id) + ('.pdf' if mime_type == 'application/pdf' else '.docx'), permit.artifact_id.hex + ('.pdf' if mime_type == 'application/pdf' else '.docx')}:
            return Result.failure('VALIDATION_ERROR')
        retry = self._gate.admit()
        if retry is not None:
            return Result.failure('TEMPORARY_FAILURE',retryability='safe')
        _, upload, failure = self._request('POST','/uploads',params={'type':'file'})
        if failure:
            return Result.failure('TEMPORARY_FAILURE',retryability='safe')
        url = upload.get('url')
        if not isinstance(url, str):
            return Result.failure('TEMPORARY_FAILURE',retryability='safe')
        try:
            parsed = urlparse(url)
            if parsed.scheme != 'https' or parsed.hostname not in self._upload_hosts or parsed.username or parsed.password or parsed.port not in {None,443}:
                return Result.failure('ACCESS_DENIED')
        except ValueError:
            return Result.failure('ACCESS_DENIED')
        try:
            request = self._client.build_request('POST',url,files={'data':(filename,data,mime_type)},timeout=self._timeout)
            # Signed upload URL is the credential. Never forward bot token/cookies.
            for header in ('Authorization','Cookie'):
                request.headers.pop(header,None)
            response = self._client.send(request,auth=None,follow_redirects=False)
            uploaded = response.json()
            token = uploaded.get('token') if isinstance(uploaded,dict) else None
            if not 200 <= response.status_code < 300 or not isinstance(token,str) or not 1 <= len(token) <= 8192:
                return Result.failure('TEMPORARY_FAILURE',retryability='safe')
        except (httpx.RequestError,ValueError):
            return Result.failure('TEMPORARY_FAILURE',retryability='safe')
        record = AttachmentTokenRecord(attachment_token_ref=uuid4(),artifact_id=permit.artifact_id,
                 owner_id=permit.owner_id,manifest_hash=permit.manifest_hash,token=token,state='uploaded',observed_at=self._clock())
        try:
            ref = self._write_attachment(record)
            if ref != record.attachment_token_ref:
                return Result.failure('ACCESS_DENIED')
        except Exception:
            # Remote orphan token is not proof of delivery and has no domain reference.
            return Result.failure('TEMPORARY_FAILURE',retryability='safe')
        return Result.success(UploadResult(**record.model_dump(exclude={'token','schema_version'})))

    def inspect_subscription(self, expected_url: str) -> SubscriptionHealth:
        retry = self._gate.admit()
        if retry is not None:
            return SubscriptionHealth(False,'local_rate_limit')
        _, data, failure = self._request('GET','/subscriptions')
        if failure:
            return SubscriptionHealth(False,'subscription_unavailable')
        subscriptions = data.get('subscriptions')
        if not isinstance(subscriptions,list):
            return SubscriptionHealth(False,'invalid_subscription_response')
        required = {'message_created','message_callback','bot_started'}
        for subscription in subscriptions:
            if isinstance(subscription,dict) and subscription.get('url') == expected_url:
                types = subscription.get('update_types')
                if isinstance(types,list) and required.issubset(types):
                    return SubscriptionHealth(True,'url_and_event_types_verified')
        return SubscriptionHealth(False,'subscription_missing_or_incomplete')
