"""Exercise actual startup composition and release persistence in PostgreSQL."""
import os
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from psycopg.conninfo import conninfo_to_dict

from test_db import db
from tsr.bootstrap import build_application
from tsr.config import Settings
from tsr.contracts import ActorContext, CommandEnvelope, StartCasePayload


def test_real_bootstrap_migrates_activates_and_restarts(db, tmp_path):
    parts = urlsplit(os.environ['TSR_TEST_DATABASE_URL'])
    query = dict(parse_qsl(parts.query))
    query['options'] = conninfo_to_dict(db.dsn)['options']
    scoped_url = urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))
    settings = Settings(database_url=scoped_url, encryption_key='78' * 32,
                        identity_hmac_key='test-bootstrap', release_root=Path(__file__).parents[1],
                        private_root=tmp_path / 'private')
    startup_db, application, files = build_application(settings)
    with startup_db.uow() as uow:
        active = uow.releases.get_active(settings.mode,bot_scope=settings.bot_scope)
    assert active is not None
    assert files.root == settings.private_root
    actor = ActorContext(owner_id=uuid4(), delivery_target_id=uuid4(), bot_scope=settings.bot_scope,
                         case_mode=settings.mode, correlation_id=uuid4())
    started = application.execute_command(CommandEnvelope(
        command_id=uuid4(), actor=actor, type='start_case',
        payload=StartCasePayload(category_ref=application.release.profile.ref, requested_role='self')))
    assert started.ok, started.error
    assert started.value.case.mode == 'demo'
    restarted_db, _, _ = build_application(settings)
    with restarted_db.uow() as uow:
        assert uow.releases.get_active(settings.mode,bot_scope=settings.bot_scope) == active
        assert uow.cases.get(started.value.case.case_id) == started.value.case


def _startup_settings(db, tmp_path, root):
    parts=urlsplit(os.environ['TSR_TEST_DATABASE_URL'])
    query=dict(parse_qsl(parts.query));query['options']=conninfo_to_dict(db.dsn)['options']
    scoped_url=urlunsplit(parts._replace(query=urlencode(query,quote_via=quote)))
    return Settings(database_url=scoped_url,encryption_key='78'*32,identity_hmac_key='test-bootstrap',
        release_root=root,private_root=tmp_path/'private')


def test_restart_loads_active_database_release_instead_of_configured_old_demo(db,tmp_path):
    from datetime import datetime,timezone
    from tsr.operations.releases import activate_release,import_release,load_demo_release
    root=Path(__file__).parents[1]
    settings=_startup_settings(db,tmp_path,root)
    startup_db,first,_=build_application(settings)
    old=load_demo_release(root)
    newer=old.manifest.model_copy(update={'release_id':'startup-switch','version':'1.1.0'})
    now=datetime.now(timezone.utc)
    assert import_release(root,newer,'test',db=startup_db,now=now).status=='staged'
    assert activate_release(newer.ref,'test','demo',now,db=startup_db,allow_synthetic_draft=True,bot_scope=settings.bot_scope).ok
    _,restarted,_=build_application(settings)
    assert restarted.release.release_ref==newer.ref
    assert restarted.release.release_ref!=first.release.release_ref
    with startup_db.uow() as uow:
        record=uow.releases.get(newer.ref)
        assert record.packages and record.packages[0].content==old.profile
        assert uow.releases.get(old.release_ref).manifest==old.manifest


def test_missing_active_package_never_falls_back_to_valid_demo(db,tmp_path):
    import shutil
    import pytest
    from datetime import datetime,timezone
    from tsr.operations.releases import activate_release,import_release,load_demo_release
    root=tmp_path/'release-root'
    repository=Path(__file__).parents[1]
    for name in ('data','schemas','assets','templates'):
        shutil.copytree(repository/name,root/name)
    old=load_demo_release(root)
    entry=next(item for item in old.manifest.files if item.kind=='CatalogPack')
    target=root/'data/catalog/active-only/catalog.json';target.parent.mkdir(parents=True)
    shutil.copyfile(root/entry.path,target)
    changed=entry.model_copy(update={'path':str(target.relative_to(root))})
    newer=old.manifest.model_copy(update={'release_id':'active-only','version':'1.1.0',
        'files':tuple(changed if item.kind=='CatalogPack' else item for item in old.manifest.files)})
    now=datetime.now(timezone.utc)
    assert import_release(root,newer,'test',db=db,now=now).status=='staged'
    assert activate_release(newer.ref,'test','demo',now,db=db,allow_synthetic_draft=True,bot_scope='tsr-demo').ok
    target.unlink()
    assert load_demo_release(root).release_ref==old.release_ref
    with pytest.raises(ValueError,match='data_release_not_ready'):
        build_application(_startup_settings(db,tmp_path,root))


def test_bootstrap_blocks_unfinished_restore_before_reading_or_activating_release(monkeypatch,tmp_path):
    import pytest
    import tsr.adapters.db
    class RestoringDatabase:
        def __init__(self,dsn,crypto,**deadlines): pass
        def migrate(self): raise AssertionError('restore barrier must stop migrations')
        def restore_ready(self): return False
        def uow(self): raise AssertionError('restore barrier must stop release access')
    monkeypatch.setattr(tsr.adapters.db,'Database',RestoringDatabase)
    settings=Settings(database_url='postgresql://fixture@localhost/fixture',encryption_key='78'*32,
        identity_hmac_key='fixture',release_root=Path(__file__).parents[1],private_root=tmp_path/'private')
    with pytest.raises(ValueError,match='restore_not_ready'):
        build_application(settings)
