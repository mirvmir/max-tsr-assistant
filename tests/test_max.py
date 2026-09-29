"""Boundary tests: no network credentials; synthetic MAX fixtures only."""
from __future__ import annotations

import json
from pathlib import Path


def test_webhook_auth_and_minimization():
    from tsr.adapters.max import parse_update, verify_secret

    assert verify_secret('fixture-secret', 'fixture-secret')
    assert not verify_secret(None, 'fixture-secret')
    assert not verify_secret('wrong', 'fixture-secret')
    update = parse_update((Path(__file__).parent / 'fixtures/max/text.json').read_bytes())
    assert update.kind == 'text'
    assert update.text == '45'
    assert not hasattr(update, 'name')
    grouped = json.loads((Path(__file__).parent / 'fixtures/max/text.json').read_text())
    grouped['message']['recipient']['chat_type'] = 'chat'
    grouped['message']['body']['text'] = 'sensitive group input'
    ignored = parse_update(json.dumps(grouped).encode())
    assert ignored.kind == 'ignored'
    assert ignored.text is None and ignored.platform_user_id is None


def test_http_refuses_unauthenticated_and_commit_failure():
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from tsr.http import create_app

    settings = SimpleNamespace(max_webhook_secret='fixture-secret', readiness_secret='ready-secret',
                               identity_hmac_key='fixture-identity-key', bot_scope='fixture', max_body_bytes=4096)
    class Unavailable:
        def uow(self):
            raise RuntimeError('database sensitive DSN must not be returned')
        def ping(self):
            return False
    client = TestClient(create_app(settings, Unavailable()))
    body = json.loads((Path(__file__).parent / 'fixtures/max/text.json').read_text())
    assert client.post('/webhooks/max', json=body).status_code == 401
    failure = client.post('/webhooks/max', json=body, headers={'X-Max-Bot-Api-Secret':'fixture-secret'})
    assert failure.status_code == 503 and 'sensitive' not in failure.text
    assert client.post('/webhooks/max', content=b'x'*4097, headers={'X-Max-Bot-Api-Secret':'fixture-secret','Content-Type':'application/json'}).status_code == 413
    assert client.get('/health/ready').status_code == 401
    assert client.get('/health/ready',headers={'X-Readiness-Secret':'ready-secret'}).status_code == 503
    assert client.get('/health/live').json() == {'status':'live'}
    assert client.get('/docs').status_code == 404
    assert set(client.app.openapi()['paths']) == {'/webhooks/max','/health/live','/health/ready'}


def test_transport_timeout_is_unknown_and_callback_receipt_has_no_message_id():
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4
    import httpx
    from tsr.contracts import CallbackAnswer, IdentityRecord, SendPermit, ViewModel, content_hash
    from tsr.adapters.max import MaxTransport, render_max_view

    now = datetime.now(timezone.utc)
    owner, target = uuid4(), uuid4()
    identity = IdentityRecord(identity_id=target, owner_id=owner, delivery_target_id=target,
                              bot_scope='fixture', lookup_key='opaque', platform_user_id='7001', platform_chat_id='8001')
    def permit(payload):
        return SendPermit(outbox_id=uuid4(), send_attempt_id=uuid4(), job_id=uuid4(), fence_token=1,
                          owner_id=owner, delivery_target_id=target, approved_at=now,
                          payload_hash=content_hash(payload), expires_at=now+timedelta(minutes=1))
    view = ViewModel(view_id=uuid4(),kind='help',title_key='help.title')
    def timeout(request):
        raise httpx.ReadTimeout('not logged', request=request)
    transport = MaxTransport(httpx.Client(transport=httpx.MockTransport(timeout)), 'fixture-token', lambda _:identity, clock=lambda:now)
    result = transport.send_view(permit(view), render_max_view(view))
    assert result.status == 'unknown' and not result.retryable
    answer = CallbackAnswer(owner_id=owner,platform_callback_id='callback-fixture',text_key='confirm',inbox_id=uuid4())
    observed = []
    def reply(request):
        observed.append(request)
        return httpx.Response(200,json={'success':True})
    transport = MaxTransport(httpx.Client(transport=httpx.MockTransport(reply)), 'fixture-token', lambda _:identity,clock=lambda:now)
    receipt = transport.answer_callback(permit(answer),answer)
    assert receipt.status == 'confirmed' and receipt.receipt.callback_id == 'callback-fixture'
    assert not hasattr(receipt.receipt,'message_id')
    assert observed[0].headers['Authorization'] == 'fixture-token'
    assert 'fixture-token' not in str(observed[0].url)
    assert observed[0].url.path == '/answers'


def test_atomic_webhook_commit_and_duplicate_with_real_postgres():
    import os
    import pytest
    import psycopg
    from psycopg.conninfo import make_conninfo
    from contextlib import contextmanager
    from types import SimpleNamespace
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from tsr.adapters.crypto import Crypto
    from tsr.adapters.db import Database
    from tsr.http import create_app

    dsn = os.getenv('TSR_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('TSR_TEST_DATABASE_URL required for PostgreSQL webhook commit test')
    schema = 'test_max_' + uuid4().hex
    with psycopg.connect(dsn) as connection:
        connection.execute(psycopg.sql.SQL('CREATE SCHEMA {}').format(psycopg.sql.Identifier(schema)))
    db = Database(make_conninfo(dsn,options='-c search_path='+schema),Crypto(b'm'*32))
    db.migrate()
    settings = SimpleNamespace(max_webhook_secret='fixture-secret',readiness_secret='ready-secret',
        identity_hmac_key='fixture-identity-key',bot_scope='fixture',max_body_bytes=4096)
    body = json.loads((Path(__file__).parent / 'fixtures/max/text.json').read_text())
    headers = {'X-Max-Bot-Api-Secret':'fixture-secret'}
    class FailedJobDatabase:
        @contextmanager
        def uow(self):
            with db.uow() as uow:
                class FailedJobs:
                    def enqueue_unique(self,_):
                        raise RuntimeError('injected durable job write failure')
                uow.work = FailedJobs()
                yield uow
    try:
        failure = TestClient(create_app(settings,FailedJobDatabase())).post('/webhooks/max',json=body,headers=headers)
        assert failure.status_code == 503
        with db.uow() as u:
            assert u.connection.execute('SELECT count(*) AS n FROM inbox_events').fetchone()['n'] == 0
            assert u.connection.execute('SELECT count(*) AS n FROM user_identities').fetchone()['n'] == 0
        client = TestClient(create_app(settings,db))
        assert client.post('/webhooks/max',json=body,headers=headers).status_code == 200
        assert client.post('/webhooks/max',json=body,headers=headers).status_code == 200
        with db.uow() as u:
            inbox = u.connection.execute('SELECT * FROM inbox_events').fetchall()
            jobs = u.connection.execute('SELECT * FROM jobs').fetchall()
            identities = u.connection.execute('SELECT * FROM user_identities').fetchall()
            assert len(inbox) == len(jobs) == len(identities) == 1
            assert identities[0]['opaque_key'] != '7001'
            assert b'Synthetic name' not in bytes(inbox[0]['ciphertext'])
            stored = u.inbox.get(inbox[0]['id'])
            assert stored.event.payload.text == '45'
            assert not hasattr(stored.event.payload,'owner_id')
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(psycopg.sql.SQL('DROP SCHEMA {} CASCADE').format(psycopg.sql.Identifier(schema)))


def test_owner_binding_rejection_and_attachment_not_ready_do_not_claim_delivery():
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4
    import httpx
    from tsr.contracts import IdentityRecord, SendPermit, ViewModel, content_hash
    from tsr.adapters.max import MaxTransport, render_max_view
    now = datetime.now(timezone.utc)
    owner, target = uuid4(),uuid4()
    view = ViewModel(view_id=uuid4(),kind='help',title_key='help.title')
    permit = SendPermit(outbox_id=uuid4(),send_attempt_id=uuid4(),job_id=uuid4(),fence_token=1,
        owner_id=owner,delivery_target_id=target,approved_at=now,payload_hash=content_hash(view),expires_at=now+timedelta(seconds=30))
    identity = IdentityRecord(identity_id=target,owner_id=uuid4(),delivery_target_id=target,
        bot_scope='fixture',lookup_key='opaque',platform_user_id='7001')
    calls = []
    def reject(request):
        calls.append(request)
        return httpx.Response(400,json={'code':'attachment.not.ready','message':'discarded'})
    transport = MaxTransport(httpx.Client(transport=httpx.MockTransport(reject)),'fixture-token',lambda _:identity,clock=lambda:now)
    assert transport.send_view(permit,render_max_view(view)).status == 'definitely_rejected'
    assert not calls
    identity = identity.model_copy(update={'owner_id':owner})
    transport = MaxTransport(httpx.Client(transport=httpx.MockTransport(reject)),'fixture-token',lambda _:identity,clock=lambda:now)
    pending = transport.send_view(permit,render_max_view(view))
    assert pending.status == 'definitely_rejected' and pending.retryable
    assert pending.error_code == 'attachment.not.ready' and pending.receipt is None


def test_upload_emits_reference_and_does_not_forward_bot_token():
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4
    import httpx
    from tsr.contracts import UploadPermit
    from tsr.adapters.max import MaxTransport
    now = datetime.now(timezone.utc)
    upload = UploadPermit(artifact_id=uuid4(),owner_id=uuid4(),manifest_hash='f'*64,
                          approved_at=now,expires_at=now+timedelta(seconds=30))
    records = []
    requests = []
    def reply(request):
        requests.append(request)
        if request.url.path == '/uploads':
            return httpx.Response(200,json={'url':'https://fu.oneme.ru/api/upload.do?signed=fixture'})
        assert request.url.host == 'fu.oneme.ru'
        assert 'authorization' not in request.headers
        assert 'cookie' not in request.headers
        assert b'name="data"' in request.content
        return httpx.Response(200,json={'fileId':123,'token':'sensitive-upload-token'})
    def write(record):
        records.append(record)
        return record.attachment_token_ref
    client = httpx.Client(transport=httpx.MockTransport(reply),headers={'Authorization':'must-be-overridden','Cookie':'must-be-stripped'})
    transport = MaxTransport(client,'fixture-token',lambda _:None,attachment_writer=write,clock=lambda:now)
    result = transport.upload_file(upload,b'%PDF-fixture','application/pdf',str(upload.artifact_id)+'.pdf')
    assert result.ok and result.value.state == 'uploaded'
    assert records[0].token == 'sensitive-upload-token'
    assert result.value.attachment_token_ref == records[0].attachment_token_ref
    assert 'sensitive-upload-token' not in result.value.model_dump_json()


def test_presenter_plain_text_does_not_expand_user_markup():
    from uuid import uuid4
    from tsr.contracts import ViewModel, ViewSection
    from tsr.adapters.max import render_max_view
    user_text = '&' * 2000
    view = ViewModel(view_id=uuid4(),kind='candidate',title_key='candidate.title',
                     sections=(ViewSection(text_key='text',parameters={'text':user_text}),))
    rendered = render_max_view(view)
    assert user_text in rendered.text
    assert len(rendered.text) <= 4000
    assert 'format' not in rendered.as_api_dict()
    assert '<b>' not in rendered.text


def test_live_transport_keeps_certificate_verification_for_default_and_custom_ca(tmp_path):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    import ssl
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from pydantic import SecretStr
    from tsr.runtime import live_transport

    settings = SimpleNamespace(max_token=SecretStr('fixture-token'),network_timeout_seconds=2,
        max_api_url='https://platform-api2.max.ru',max_file_bytes=1024,max_ca_bundle=None)
    bindings = SimpleNamespace(recipient=lambda _:None,attachment=lambda *_:None,
        write_attachment=lambda _:None,message=lambda *_:False)
    default_transport = live_transport(settings,bindings)
    default_context = default_transport._client._transport._pool._ssl_context
    assert isinstance(default_context,ssl.SSLContext)
    assert default_context.verify_mode == ssl.CERT_REQUIRED and default_context.check_hostname
    assert default_context.get_ca_certs()
    default_transport._client.close()

    key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Local MAX test CA')])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256()))
    ca = tmp_path / 'test-ca.pem'
    ca.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    settings.max_ca_bundle = ca
    custom_transport = live_transport(settings,bindings)
    custom_context = custom_transport._client._transport._pool._ssl_context
    assert custom_context.verify_mode == ssl.CERT_REQUIRED and custom_context.check_hostname
    assert custom_context.get_ca_certs() == ssl.create_default_context(cafile=str(ca)).get_ca_certs()
    assert custom_context.get_ca_certs() != default_context.get_ca_certs()
    custom_transport._client.close()
