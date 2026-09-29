"""Verified MAX envelope parsing; raw update structures stay in this adapter."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
from uuid import uuid4


def verify_secret(supplied: str | None, expected: str | None) -> bool:
    """Reject an absent configuration and compare opaque header bytes in constant time."""
    return bool(expected and supplied) and hmac.compare_digest(supplied.encode(), expected.encode())


@dataclass(frozen=True)
class VerifiedUpdate:
    kind: str
    occurred_at: datetime
    event_key: str
    platform_user_id: str | None = None
    platform_chat_id: str | None = None
    text: str | None = None
    action_handle: str | None = None
    platform_callback_id: str | None = None
    start_payload: str | None = None
    reply_to_message_id: str | None = None


def _object(value):
    if not isinstance(value, dict):
        raise ValueError('invalid_update')
    return value


def _integer(value):
    if type(value) is not int or not -(2**63) <= value < 2**63:
        raise ValueError('invalid_update')
    return value


def _string(value, maximum, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError('invalid_update')
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('invalid_update')
        result[key] = value
    return result


def parse_update(body: bytes, max_text_chars: int = 4000) -> VerifiedUpdate:
    """Validate required fields; unsupported and non-private updates lose all content."""
    try:
        update = _object(json.loads(body, object_pairs_hook=_unique_object))
        kind = _string(update.get('update_type'), 64)
        timestamp = _integer(update.get('timestamp'))
        occurred = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
        # Only a digest leaves the parser for unsupported events; HMAC before storage.
        ignored = VerifiedUpdate('ignored', occurred, 'ignored:' + hashlib.sha256(body).hexdigest())
        if kind == 'bot_started':
            user = _object(update.get('user'))
            user_id = str(_integer(user.get('user_id')))
            chat_id = str(_integer(update.get('chat_id')))
            if user.get('is_bot') is True or user.get('user_id') <= 0:
                return ignored
            payload = _string(update.get('payload'), 128, nullable=True)
            return VerifiedUpdate('start', occurred, f'start:{timestamp}:{user_id}:{chat_id}',
                                  user_id, chat_id, start_payload=payload if payload in {'start','help','resume'} else None)
        if kind not in {'message_created', 'message_callback'}:
            return ignored
        message = update.get('message')
        if message is None and kind == 'message_callback':
            return ignored  # No original private message: no trusted delivery context.
        message = _object(message)
        recipient = _object(message.get('recipient'))
        if recipient.get('chat_type') not in {'dialog', 'chat', 'channel'}:
            raise ValueError('invalid_update')
        if recipient['chat_type'] != 'dialog':
            return ignored
        chat_id = str(_integer(recipient.get('chat_id')))
        if kind == 'message_callback':
            callback = _object(update.get('callback'))
            user = _object(callback.get('user'))
            callback_id = _string(callback.get('callback_id'), 512)
            handle = _string(callback.get('payload'), 128)
            user_id = str(_integer(user.get('user_id')))
            if user.get('is_bot') is True or user.get('user_id') <= 0:
                return ignored
            return VerifiedUpdate('callback', occurred, 'callback:' + callback_id, user_id, chat_id,
                                  action_handle=handle, platform_callback_id=callback_id)
        sender = _object(message.get('sender'))
        user_id = str(_integer(sender.get('user_id')))
        if sender.get('is_bot') is True or sender.get('user_id') <= 0:
            return ignored
        content = message.get('body')
        if content is None:
            return ignored
        content = _object(content)
        mid = _string(content.get('mid'), 512)
        text = _string(content.get('text'), max_text_chars, nullable=True) if content.get('text') != '' else None
        if text is None:
            return ignored
        # Forward/link metadata and all attachments deliberately excluded.
        return VerifiedUpdate('text', occurred, 'message:' + mid, user_id, chat_id, text=text)
    except (KeyError, TypeError, OverflowError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('invalid_update') from exc


def identity_lookup(bot_scope: str, platform_user_id: str | None, key: bytes) -> str:
    if not key:
        raise ValueError('identity_key_missing')
    return hmac.new(key, json.dumps(['identity-v1', bot_scope, platform_user_id], separators=(',', ':')).encode(), hashlib.sha256).hexdigest()


def normalize_update(update: VerifiedUpdate, identity, *, received_at: datetime, key: bytes):
    from tsr.contracts import CallbackEvent, IgnoredEvent, NormalizedEvent, Result, StartEvent, TextEvent

    if update.kind == 'text':
        payload = TextEvent(text=update.text, reply_to_message_id=update.reply_to_message_id)
    elif update.kind == 'callback':
        payload = CallbackEvent(platform_callback_id=update.platform_callback_id, action_handle=update.action_handle)
    elif update.kind == 'start':
        payload = StartEvent(payload=update.start_payload)
    else:
        payload = IgnoredEvent()
    dedupe = hmac.new(key, json.dumps(['event-v1', identity.bot_scope, identity.lookup_key, update.event_key], separators=(',', ':')).encode(), hashlib.sha256).hexdigest()
    return Result.success(NormalizedEvent(inbox_id=uuid4(), bot_scope=identity.bot_scope, dedupe_key=dedupe,
                         owner_id=identity.owner_id, delivery_target_id=identity.delivery_target_id,
                         kind=update.kind, occurred_at=update.occurred_at, received_at=received_at, payload=payload))


def persist_update(db, settings, update: VerifiedUpdate, received_at: datetime) -> bool:
    """Atomically bind identity, insert minimized inbox, and enqueue work before ACK."""
    from tsr.contracts import IdentityRecord, InboxRecord, WorkRef

    key = secret_value(settings.identity_hmac_key).encode()
    lookup = identity_lookup(settings.bot_scope, update.platform_user_id, key)
    with db.uow() as uow:
        identity = uow.identities.find(settings.bot_scope, lookup)
        if identity is None:
            target_id = uuid4()
            identity = uow.identities.upsert(IdentityRecord(identity_id=target_id, owner_id=uuid4(),
                       delivery_target_id=target_id, bot_scope=settings.bot_scope, lookup_key=lookup,
                       platform_user_id=update.platform_user_id, platform_chat_id=update.platform_chat_id))
        # A conflicting private target cannot redirect an already bound owner.
        if identity.platform_user_id != update.platform_user_id or identity.platform_chat_id != update.platform_chat_id:
            raise ValueError('identity_binding_conflict')
        event = normalize_update(update, identity, received_at=received_at, key=key).value
        _, inserted = uow.inbox.insert_unique(InboxRecord(inbox_id=event.inbox_id, owner_id=identity.owner_id,
                    bot_scope=settings.bot_scope, event_key=event.dedupe_key, event=event,
                    status='received', received_at=received_at))
        if inserted:
            uow.work.enqueue_unique(WorkRef(job_id=uuid4(), kind='process_inbox', payload_ref=event.inbox_id,
                       owner_id=identity.owner_id, case_id=None, case_revision=None,
                       deletion_epoch=0, dedupe_key=event.dedupe_key))
        uow.commit()
    return inserted


def secret_value(value) -> str | None:
    return value.get_secret_value() if hasattr(value, 'get_secret_value') else value
