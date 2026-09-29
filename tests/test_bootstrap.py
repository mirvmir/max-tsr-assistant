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
        active = uow.releases.get_active(settings.mode)
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
        assert uow.releases.get_active(settings.mode) == active
        assert uow.cases.get(started.value.case.case_id) == started.value.case
