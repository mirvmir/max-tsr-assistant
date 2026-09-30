"""Infrastructure setup. Importing this module never reads secrets."""
from __future__ import annotations

import os
import secrets
from pathlib import Path


def generate_demo_secrets(root: Path) -> tuple[str, ...]:
    """Initialize local secret files without ever rotating an existing key."""
    root = Path(root)
    if root.is_symlink():
        raise ValueError("Secret directory must not be a symbolic link")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    values = {
        "encryption_key": secrets.token_hex(32),
        "identity_hmac_key": secrets.token_hex(32),
        "webhook_secret": secrets.token_urlsafe(32),
        "health_token": secrets.token_urlsafe(32),
        "max_bot_token": "",
    }
    for name, value in values.items():
        path = root / name
        if path.is_symlink():
            raise ValueError("Secret files must not be symbolic links")
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value + "\n")
        # Parent is private (0700). Individual read-only Docker secret mounts
        # must be readable by the application's unprivileged container UID.
        path.chmod(0o644)
    return tuple(values)


def build_application(settings):
    """Compose from the active immutable database release, initializing only an empty mode."""
    from tsr.adapters.crypto import Crypto
    from tsr.adapters.db import Database
    from tsr.adapters.files import PrivateFiles
    from tsr.application import Application
    from tsr.contracts import LifecycleRecord, ensure_active_release_ready, runtime_dependency_refs
    from datetime import datetime, timezone
    from tsr.operations.releases import load_release, import_release, activate_release, _validated_record

    crypto = Crypto(settings.encryption_key_bytes)
    db = Database(settings.database_url, crypto,
        connect_timeout_seconds=settings.db_connect_timeout_seconds,
        statement_timeout_ms=settings.db_statement_timeout_ms,
        lock_timeout_ms=settings.db_lock_timeout_ms,
        idle_transaction_timeout_ms=settings.db_idle_transaction_timeout_ms,
        backup_snapshot_timeout_ms=settings.db_backup_snapshot_timeout_ms)
    if not db.restore_ready():
        raise ValueError("restore_not_ready")
    db.migrate()
    with db.uow() as uow:
        if not uow.restore_ready():
            raise ValueError("restore_not_ready")
        active = uow.releases.get_active(settings.mode, bot_scope=settings.bot_scope)
    try:
        if active is None:
            initial = load_release(settings.release_root, settings.release_manifest)
            staged = import_release(settings.release_root, initial.manifest, "startup", db=db)
            if staged.status != "staged":
                raise ValueError("data_release_not_ready")
            activated = activate_release(initial.release_ref, "startup", settings.mode,
                datetime.now(timezone.utc), db=db, allow_synthetic_draft=settings.allow_synthetic_draft,
                only_if_empty=True, bot_scope=settings.bot_scope)
            if not activated.ok:
                raise ValueError("data_release_not_ready")
            with db.uow() as uow:
                if not uow.restore_ready():
                    raise ValueError("restore_not_ready")
                active = uow.releases.get_active(settings.mode, bot_scope=settings.bot_scope)
        if active is None:
            raise ValueError("data_release_not_ready")
        # IO and schema/content validation are outside the lifecycle transaction.
        release = load_release(settings.release_root, active)
        checked = active
        if not active.packages:
            # Legacy metadata is immutable. Derive its exact pinned payloads only
            # for lifecycle backfill; never replace the stored content or version.
            derived = _validated_record(settings.release_root, active.manifest, active.validated_at)
            checked = active.model_copy(update={"packages": derived.packages})
        now = datetime.now(timezone.utc)
        with db.uow() as uow:
            if not uow.restore_ready():
                raise ValueError("restore_not_ready")
            current = uow.releases.get_active(settings.mode, lock=True, bot_scope=settings.bot_scope)
            if current is None or current.ref != active.ref or current.content_hash != active.content_hash:
                raise ValueError("data_release_not_ready")
            uow.releases.ensure_lifecycles(checked)
            states = tuple(uow.releases.read_lifecycle(ref, lock=True) for ref in runtime_dependency_refs(checked))
            if any(state is None for state in states):
                raise ValueError("data_release_not_ready")
            ready = ensure_active_release_ready(checked, states, settings.mode, now,
                allow_synthetic_draft=settings.allow_synthetic_draft)
            if not ready.ok:
                raise ValueError("data_release_not_ready")
            route_state = next(state for state in states if state.ref == release.route.ref)
            release = release.model_copy(update={"route_lifecycle": LifecycleRecord(
                ref=route_state.ref,status="active",changed_at=None,reason_code=route_state.reason_code)})
            uow.commit()
    except (ValueError, OSError, TypeError) as exc:
        message="restore_not_ready" if str(exc)=="restore_not_ready" else "data_release_not_ready"
        raise ValueError(message) from None
    application = Application(db, release, settings)
    files = PrivateFiles(settings.private_root, crypto, max_output_bytes=settings.max_file_bytes)
    return db, application, files
