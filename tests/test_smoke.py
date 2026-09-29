from pathlib import Path
import os

import pytest
from pydantic import SecretStr

from test_db import db
from tsr.adapters.files import PrivateFiles
from tsr.application import Application
from tsr.config import Settings
from tsr.operations.releases import load_demo_release


@pytest.mark.parametrize("branch,expected", [("purchase", 1), ("support", 4)])
def test_both_demo_branches_through_worker_to_download(db, tmp_path, branch, expected):
    from tsr.demo import run_demo_scenario

    root = Path(__file__).parents[1]
    settings = Settings(database_url=os.environ["TSR_TEST_DATABASE_URL"], encryption_key=SecretStr("11" * 32),
                        identity_hmac_key=SecretStr("demo-only-hmac"),
                        private_root=tmp_path / "private", release_root=root, release_commit="test-commit")
    application = Application(db, load_demo_release(root), settings)
    files = PrivateFiles(settings.private_root, db.crypto)
    report = run_demo_scenario(settings, db, application, files, branch, tmp_path / "downloads")
    assert report.bundle_status == "ready"
    assert len(report.downloads) == expected
    assert report.gap_minor == 2_000_000
    assert report.manifest_hash == report.preview_hash
    for path in report.downloads:
        assert path.read_bytes().startswith(b"PK" if path.suffix == ".docx" else b"%PDF-")
    assert tuple(files.root.glob("*.blob"))
    assert all(not path.read_bytes().startswith(b"%PDF-") for path in files.root.glob("*.blob"))
