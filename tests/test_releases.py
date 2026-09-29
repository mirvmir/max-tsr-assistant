from pathlib import Path
import shutil

from tsr.operations.releases import load_demo_release, validate_release

ROOT = Path(__file__).parents[1]


def test_demo_release_is_explicitly_synthetic_and_complete():
    release = load_demo_release(ROOT)
    assert len(release.offers) >= 5
    assert len({offer.supplier_id for offer in release.offers}) >= 2
    assert all(offer.data_kind == 'synthetic' for offer in release.offers)
    assert release.profile.review.status == 'draft'
    assert release.route.review.status == 'draft'
    assert release.route.region_code == 'ru-alt'
    assert validate_release(ROOT, release.manifest).status == 'valid'


def test_release_hash_tampering_and_path_escape_rejected(tmp_path):
    release = load_demo_release(ROOT)
    shutil.copytree(ROOT / 'data', tmp_path / 'data')
    shutil.copytree(ROOT / 'schemas', tmp_path / 'schemas')
    entry = release.manifest.files[0]
    target = tmp_path / entry.path
    target.write_bytes(target.read_bytes() + b' ')
    report = validate_release(tmp_path, release.manifest)
    assert report.status == 'rejected'
    assert any(error.code == 'HASH_MISMATCH' for error in report.errors)
    bad_entry = entry.model_copy(update={'path':'../outside.json'})
    bad_manifest = release.manifest.model_copy(update={'files':(bad_entry,*release.manifest.files[1:])})
    assert validate_release(tmp_path,bad_manifest).status == 'rejected'

from test_db import db


def test_atomic_demo_activation_cannot_promote_draft_or_revoked(db):
    from datetime import datetime,timezone
    from tsr.operations.releases import import_release,activate_release,revoke_package
    release=load_demo_release(ROOT)
    now=datetime.now(timezone.utc)
    report=import_release(ROOT,release.manifest,'test',db=db,now=now)
    assert report.status=='staged'
    assert not activate_release(release.release_ref,'test','pilot',now,db=db,allow_synthetic_draft=True).ok
    denied=activate_release(release.release_ref,'test','demo',now,db=db)
    assert not denied.ok
    activated=activate_release(release.release_ref,'test','demo',now,db=db,allow_synthetic_draft=True)
    assert activated.ok
    with db.uow() as uow:
        assert uow.releases.get_active('demo').manifest.review.status=='draft'
    assert revoke_package(release.release_ref,'test-revoke','test',now,db=db).ok
    assert activate_release(release.release_ref,'test','demo',now,db=db,allow_synthetic_draft=True).error.code=='DATA_REVOKED'
