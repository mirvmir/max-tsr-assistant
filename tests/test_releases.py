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


def test_generic_loader_uses_exact_manifest_without_changing_old_fixture():
    from tsr.operations.releases import load_release
    from tsr.contracts import VersionRef
    old=load_demo_release(ROOT)
    alternate=old.manifest.model_copy(update={'release_id':'alternate-demo','version':'1.1.0'})
    loaded=load_release(ROOT,alternate)
    assert loaded.release_ref==VersionRef(id='alternate-demo',version='1.1.0')
    assert loaded.offers==old.offers and loaded.profile==old.profile
    assert load_demo_release(ROOT).release_ref==old.release_ref


def _record(release, now, *, mixed=False, public_draft=False):
    from datetime import timedelta
    from tsr.contracts import ReleaseRecord, ReleasePackage, Review, content_hash
    reviewed=Review(status='reviewed',reviewer_id='technical-source-audit',reviewed_at=now-timedelta(hours=1),review_due_at=now+timedelta(days=30))
    catalog=release.catalog
    if mixed:
        review=Review() if public_draft else reviewed
        catalog=catalog.model_copy(update={'data_kind':'public_snapshot','review':review,
            'offers':tuple(item.model_copy(update={'data_kind':'public_snapshot','review':review}) for item in catalog.offers)})
    contents=(('CategoryProfile',release.profile),('CatalogPack',catalog),('RoutePack',release.route),
        ('SourcesRegistry',release.sources),('TemplateRegistry',release.templates))
    packages=tuple(ReleasePackage(kind=kind,ref=item.ref,content=item) for kind,item in contents)
    return ReleaseRecord(ref=release.release_ref,manifest=release.manifest,validated_at=now,
        packages=packages,assets_verified=True,content_hash=content_hash(release.manifest))


def test_mixed_release_policy_never_allows_public_draft_and_package_lifecycle_is_checked():
    from datetime import datetime, timedelta, timezone
    from tsr.contracts import ReleaseLifecycle
    from tsr.operations.releases import check_release_policy, ensure_active_release_ready, runtime_dependency_refs
    now=datetime.now(timezone.utc)
    release=load_demo_release(ROOT)
    record=_record(release,now,mixed=True)
    assert check_release_policy(record,'demo',now,allow_synthetic_draft=True).ok
    assert not check_release_policy(record,'pilot',now,allow_synthetic_draft=True).ok
    draft=_record(release,now,mixed=True,public_draft=True)
    assert check_release_policy(draft,'demo',now,allow_synthetic_draft=True).error.code=='DATA_NOT_READY'
    refs=runtime_dependency_refs(record)
    lifecycles=tuple(ReleaseLifecycle(ref=ref) for ref in refs)
    assert ensure_active_release_ready(record,lifecycles,'demo',now,allow_synthetic_draft=True).ok
    route=release.route.ref
    revoked=tuple(item.model_copy(update={'revoked':True}) if item.ref==route else item for item in lifecycles)
    assert ensure_active_release_ready(record,revoked,'demo',now,allow_synthetic_draft=True).error.code=='DATA_REVOKED'
    expired=tuple(item.model_copy(update={'review_due_at':now-timedelta(seconds=1)}) if item.ref==release.catalog_ref else item for item in lifecycles)
    assert ensure_active_release_ready(record,expired,'demo',now,allow_synthetic_draft=True).error.code=='DATA_EXPIRED'
    assert ensure_active_release_ready(record,lifecycles[:-1],'demo',now,allow_synthetic_draft=True).error.code=='DATA_NOT_READY'


def test_pilot_policy_excludes_synthetic_test_inputs_but_requires_all_runtime_reviews():
    import json
    from datetime import datetime,timedelta,timezone
    from tsr.contracts import DemoCasePack, ReleasePackage, Review, content_hash
    from tsr.operations.releases import check_release_policy
    now=datetime.now(timezone.utc)
    release=load_demo_release(ROOT)
    base=_record(release,now)
    reviewed=Review(status='reviewed',reviewer_id='test-fixture-reviewer',reviewed_at=now-timedelta(hours=1),review_due_at=now+timedelta(days=1))
    packages=[]
    for item in base.packages:
        content=item.content.model_copy(update={'data_kind':'public_snapshot','review':reviewed})
        if item.kind=='CatalogPack': content=content.model_copy(update={'offers':tuple(offer.model_copy(update={'data_kind':'public_snapshot','review':reviewed}) for offer in content.offers)})
        if item.kind=='SourcesRegistry': content=content.model_copy(update={'sources':tuple(source.model_copy(update={'data_kind':'public_snapshot','review':reviewed}) for source in content.sources)})
        packages.append(item.model_copy(update={'content':content}))
    entry=next(entry for entry in release.manifest.files if entry.kind=='DemoCasePack')
    demo=DemoCasePack.model_validate(json.loads((ROOT/entry.path).read_bytes()))
    packages.append(ReleasePackage(kind='DemoCasePack',ref=entry.ref,content=demo))
    manifest=release.manifest.model_copy(update={'data_kind':'public_snapshot','review':reviewed})
    record=base.model_copy(update={'manifest':manifest,'packages':tuple(packages),'content_hash':content_hash(manifest)})
    assert check_release_policy(record,'pilot',now).ok
    route=next(item for item in record.packages if item.kind=='RoutePack')
    invalid=route.model_copy(update={'content':route.content.model_copy(update={'review':Review()})})
    record=record.model_copy(update={'packages':tuple(invalid if item.kind=='RoutePack' else item for item in record.packages)})
    assert check_release_policy(record,'pilot',now).error.code=='DATA_NOT_READY'


def test_package_revoke_expiry_and_rollback_cannot_restore_access(db):
    from datetime import datetime,timedelta,timezone
    from tsr.operations.releases import activate_release,import_release,revoke_package,rollback_release
    release=load_demo_release(ROOT)
    now=datetime.now(timezone.utc)
    assert import_release(ROOT,release.manifest,'test',db=db,now=now).status=='staged'
    assert activate_release(release.release_ref,'test','demo',now,db=db,allow_synthetic_draft=True).ok
    with db.uow() as uow:
        uow.connection.execute('UPDATE data_lifecycle SET review_due_at=%s WHERE id=%s AND version=%s',
            (now-timedelta(seconds=1),release.catalog_ref.id,release.catalog_ref.version))
        uow.commit()
    assert rollback_release(release.release_ref,'test','demo',now,db=db,allow_synthetic_draft=True).error.code=='DATA_EXPIRED'
    assert revoke_package(release.route.ref,'route-revocation','test',now,db=db).ok
    assert activate_release(release.release_ref,'test','demo',now,db=db,allow_synthetic_draft=True).error.code in {'DATA_REVOKED','DATA_EXPIRED'}
    with db.uow() as uow:
        assert uow.releases.read_lifecycle(release.route.ref).revoked
        assert uow.releases.get_active('demo').ref==release.release_ref


def test_golden_expected_requires_the_scenario_result_and_strict_money_types(tmp_path):
    import json
    import hashlib
    release=load_demo_release(ROOT)
    for name in ('data','schemas','assets','templates'):
        shutil.copytree(ROOT/name,tmp_path/name)
    entry=next(item for item in release.manifest.files if item.kind=='GoldenCasePack')
    target=tmp_path/entry.path
    original=json.loads(target.read_bytes())
    variants=({'irrelevant':123}, {**original['cases'][0]['expected'],'class':1},
        {**original['cases'][0]['expected'],'coverage_minor':True},
        {**original['cases'][0]['expected'],'gap_minor':-1},
        {**original['cases'][0]['expected'],'field_key':['seat_width']},
        {**original['cases'][0]['expected'],'error_code':['DATA_NOT_READY']})
    for expected in variants:
        changed=json.loads(json.dumps(original));changed['cases'][0]['expected']=expected
        raw=json.dumps(changed,ensure_ascii=False).encode('utf-8');target.write_bytes(raw)
        changed_entry=entry.model_copy(update={'sha256':hashlib.sha256(raw).hexdigest()})
        manifest=release.manifest.model_copy(update={'files':tuple(changed_entry if item.kind=='GoldenCasePack' else item for item in release.manifest.files)})
        report=validate_release(tmp_path,manifest)
        assert report.status=='rejected'
        assert any('.expected' in error.path for error in report.errors)


def test_template_import_accepts_new_ids_and_versions_but_rejects_ambiguous_kind_format(tmp_path):
    import json
    import hashlib
    release=load_demo_release(ROOT)
    for name in ('data','schemas','assets','templates'):
        shutil.copytree(ROOT/name,tmp_path/name)
    entry=next(item for item in release.manifest.files if item.kind=='TemplateRegistry')
    target=tmp_path/entry.path
    registry=json.loads(target.read_bytes())
    registry['templates'][0]['template_id']='approved-purchase-card'
    registry['templates'][0]['version']='2.1.0'
    def validate(payload):
        raw=json.dumps(payload,ensure_ascii=False).encode('utf-8');target.write_bytes(raw)
        changed=entry.model_copy(update={'sha256':hashlib.sha256(raw).hexdigest()})
        manifest=release.manifest.model_copy(update={'files':tuple(changed if item.kind=='TemplateRegistry' else item for item in release.manifest.files)})
        return validate_release(tmp_path,manifest)
    assert validate(registry).status=='valid'
    missing=json.loads(json.dumps(registry));missing['templates']=missing['templates'][1:]
    assert any(error.code=='MISSING_ARTIFACT' for error in validate(missing).errors)
    duplicate=json.loads(json.dumps(registry['templates'][0]))
    duplicate['template_id']='another-purchase-card'
    registry['templates'].append(duplicate)
    rejected=validate(registry)
    assert rejected.status=='rejected'
    assert any(error.code=='AMBIGUOUS_ARTIFACT' for error in rejected.errors)
