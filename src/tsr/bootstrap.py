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
    from tsr.adapters.crypto import Crypto
    from tsr.adapters.db import Database
    from tsr.adapters.files import PrivateFiles
    from tsr.application import Application
    from datetime import datetime, timezone
    from tsr.operations.releases import load_demo_release, import_release, activate_release

    crypto = Crypto(settings.encryption_key_bytes)
    db = Database(settings.database_url, crypto)
    db.migrate()
    release = load_demo_release(settings.release_root)
    staged = import_release(settings.release_root, release.manifest, "startup", db=db)
    if staged.status != "staged":
        raise ValueError("data_release_not_ready")
    with db.uow() as uow:
        active = uow.releases.get_active(settings.mode)
    if active is None:
        activated = activate_release(release.release_ref, "startup", settings.mode,
                                     datetime.now(timezone.utc), db=db,
                                     allow_synthetic_draft=settings.allow_synthetic_draft)
        if not activated.ok:
            raise ValueError("data_release_not_ready")
    application = Application(db, release, settings)
    files = PrivateFiles(settings.private_root, crypto, max_output_bytes=settings.max_file_bytes)
    return db, application, files
