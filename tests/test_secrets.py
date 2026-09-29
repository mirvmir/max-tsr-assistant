from pathlib import Path

import pytest


def test_initializing_secrets_preserves_existing_encryption_key(tmp_path):
    from tsr.bootstrap import generate_demo_secrets

    names = generate_demo_secrets(tmp_path / "secrets")
    key = (tmp_path / "secrets" / "encryption_key").read_bytes()
    assert len(bytes.fromhex(key.decode().strip())) == 32
    assert "max_bot_token" in names
    generate_demo_secrets(tmp_path / "secrets")
    assert (tmp_path / "secrets" / "encryption_key").read_bytes() == key


def test_initializing_secrets_rejects_symlink_destination(tmp_path):
    from tsr.bootstrap import generate_demo_secrets

    target = tmp_path / "untouched"
    target.mkdir()
    (tmp_path / "secrets").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        generate_demo_secrets(tmp_path / "secrets")
    assert tuple(target.iterdir()) == ()
