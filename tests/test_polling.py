"""Local bridge reliability: never acknowledge an event before durable ingress."""
import json
import os
from pathlib import Path
import stat

import httpx
import pytest

from tools.poll_max import PollingError, State, api_json, poll_once, require_no_subscription


def state_at(tmp_path, identity='fixture-bot'):
    return State(tmp_path / 'polling.state', b'p' * 32, identity)


def step(api, local, state, **kwargs):
    return poll_once(api, local, base='https://platform-api2.max.ru', token='fixture-token',
                     endpoint='http://app:8080/webhooks/max', secret='fixture-secret', state=state, **kwargs)


def test_restart_replays_encrypted_pending_batch_without_advancing_remote_marker(tmp_path):
    state = state_at(tmp_path)
    remote_requests, delivered = [], []
    batch = [{'update_type': 'fixture', 'text': 'private-example'}, {'update_type': 'fixture2'}]

    def remote(request):
        remote_requests.append(request)
        assert request.headers['Authorization'] == 'fixture-token'
        assert 'fixture-token' not in str(request.url)
        return httpx.Response(200, json={'updates': batch, 'marker': 123})

    def interrupted(request):
        assert 'Authorization' not in request.headers
        assert request.headers['X-Max-Bot-Api-Secret'] == 'fixture-secret'
        delivered.append(json.loads(request.content))
        return httpx.Response(200, json={'status': 'accepted'}) if len(delivered) == 1 else httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(remote)) as api, \
         httpx.Client(transport=httpx.MockTransport(interrupted)) as local:
        with pytest.raises(PollingError, match='local_webhook_http_503'):
            step(api, local, state)
        assert state.read()['marker'] is None
        assert state.read()['pending'] == batch
        assert b'private-example' not in state.path.read_bytes()

        replayed = []
        def recovered(request):
            replayed.append(json.loads(request.content))
            return httpx.Response(200, json={'status': 'accepted'})
        with httpx.Client(transport=httpx.MockTransport(recovered)) as retry:
            assert step(api, retry, state_at(tmp_path)) == 2
        assert replayed == batch
        assert len(remote_requests) == 1
        assert state.read() == {'marker': 123, 'pending': [], 'next_marker': None}


def test_poll_uses_saved_cursor_and_preserves_it_on_empty_null_marker(tmp_path):
    state = state_at(tmp_path)
    state.save({'marker': 42, 'pending': [], 'next_marker': None})
    def remote(request):
        assert request.url.params['marker'] == '42'
        assert request.url.params['types'] == 'bot_started,message_created,message_callback'
        return httpx.Response(200, json={'updates': [], 'marker': None})
    with httpx.Client(transport=httpx.MockTransport(remote)) as api, httpx.Client() as local:
        assert step(api, local, state) == 0
    assert state.read()['marker'] == 42


@pytest.mark.parametrize('response', [{'status': 'live'}, {}, None])
def test_http_200_without_durable_ack_keeps_pending_batch(tmp_path, response):
    state = state_at(tmp_path)
    state.save({'marker': 10, 'pending': [{'update_type': 'fixture'}], 'next_marker': 11})
    with httpx.Client() as api, httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=response))) as local:
        with pytest.raises(PollingError, match='local_webhook_invalid_ack'):
            step(api, local, state)
    assert state.read()['marker'] == 10
    assert len(state.read()['pending']) == 1


def test_saved_state_is_bound_to_bot_and_authenticated(tmp_path):
    state = state_at(tmp_path)
    state.save({'marker': 1, 'pending': [], 'next_marker': None})
    with pytest.raises(PollingError, match='polling_state_invalid'):
        state_at(tmp_path, identity='other-bot').read()
    state.path.write_bytes(state.path.read_bytes()[:-1] + b'!')
    with pytest.raises(PollingError, match='polling_state_invalid'):
        state.read()


def test_existing_webhook_is_never_removed_by_local_bridge():
    require_no_subscription({'subscriptions': []})
    with pytest.raises(PollingError, match='webhook_already_registered'):
        require_no_subscription({'subscriptions': [{'url': 'https://existing.example/hook'}]})
    with pytest.raises(PollingError, match='max_invalid_subscriptions'):
        require_no_subscription({})


def test_api_errors_and_redirects_do_not_include_response_secrets():
    for status in (302, 401, 429, 500):
        with httpx.Client(transport=httpx.MockTransport(
                lambda _: httpx.Response(status, text='fixture-token sensitive response',
                                        headers={'Location': 'https://other.example'}))) as api:
            with pytest.raises(PollingError) as error:
                api_json(api, 'https://platform-api2.max.ru', '/me', 'fixture-token')
            assert str(error.value) == f'max_http_{status}'


def test_missing_next_marker_does_not_forward_or_overwrite_cursor(tmp_path):
    state = state_at(tmp_path)
    state.save({'marker': 12, 'pending': [], 'next_marker': None})
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={'updates': [{'update_type': 'fixture'}], 'marker': None}))) as api, \
         httpx.Client() as local:
        with pytest.raises(PollingError, match='max_missing_marker'):
            step(api, local, state)
    assert state.read()['marker'] == 12


@pytest.mark.parametrize('status,reason', [(400, 'invalid_update'), (413, 'body_too_large')])
def test_known_permanent_rejection_does_not_block_next_update(tmp_path, capsys, status, reason):
    state = state_at(tmp_path)
    batch = [{'text': 'private-rejected-example'}, {'text': 'next'}]
    state.save({'marker': 42, 'pending': batch, 'next_marker': 43})

    def local(request):
        if json.loads(request.content) == batch[0]:
            return httpx.Response(status, json={'detail': reason})
        return httpx.Response(200, json={'status': 'accepted'})

    with httpx.Client() as api, httpx.Client(transport=httpx.MockTransport(local)) as receiver:
        assert step(api, receiver, state) == 1
    assert state_at(tmp_path).read() == {
        'marker': 43, 'pending': [], 'next_marker': None, 'rejected_counts': {reason: 1}}
    assert b'private-rejected-example' not in state.path.read_bytes()
    log = capsys.readouterr().out
    assert reason in log
    assert 'private-rejected-example' not in log and 'fixture-secret' not in log

    def remote(request):
        assert request.url.params['marker'] == '43'
        return httpx.Response(200, json={'updates': [], 'marker': None})
    with httpx.Client(transport=httpx.MockTransport(remote)) as api, httpx.Client() as receiver:
        assert step(api, receiver, state_at(tmp_path)) == 0
    assert state.read()['rejected_counts'] == {reason: 1}


def test_update_above_local_byte_limit_does_not_block_next_update(tmp_path):
    state = state_at(tmp_path)
    state.save({'marker': 42, 'pending': [{'text': 'private-example' * 100}, {}], 'next_marker': 43})
    def local(request):
        assert json.loads(request.content) == {}
        return httpx.Response(200, json={'status': 'accepted'})
    with httpx.Client() as api, httpx.Client(transport=httpx.MockTransport(local)) as receiver:
        assert step(api, receiver, state, max_body_bytes=64) == 1
    assert state.read() == {'marker': 43, 'pending': [], 'next_marker': None,
                            'rejected_counts': {'body_too_large': 1}}


def test_rejected_event_is_not_retried_after_later_temporary_failure(tmp_path):
    state = state_at(tmp_path)
    batch = [{'text': 'invalid'}, {'text': 'retry'}]
    state.save({'marker': 42, 'pending': batch, 'next_marker': 43})
    def interrupted(request):
        return (httpx.Response(400, json={'detail': 'invalid_update'})
                if json.loads(request.content) == batch[0] else httpx.Response(503))
    with httpx.Client() as api, httpx.Client(transport=httpx.MockTransport(interrupted)) as receiver:
        with pytest.raises(PollingError, match='local_webhook_http_503'):
            step(api, receiver, state)
    assert state_at(tmp_path).read() == {'marker': 42, 'pending': [batch[1]], 'next_marker': 43,
                                        'rejected_counts': {'invalid_update': 1}}
    def recovered(request):
        assert json.loads(request.content) == batch[1]
        return httpx.Response(200, json={'status': 'accepted'})
    with httpx.Client() as api, httpx.Client(transport=httpx.MockTransport(recovered)) as receiver:
        assert step(api, receiver, state_at(tmp_path)) == 1
    assert state.read()['marker'] == 43
    assert state.read()['rejected_counts'] == {'invalid_update': 1}


@pytest.mark.parametrize('status,body', [
    (400, {'detail': 'other_error'}), (400, None), (413, {'detail': 'proxy_limit'}),
    (401, {'detail': 'unauthorized'}), (403, {}), (415, {'detail': 'unsupported_media_type'}),
    (429, {}), (503, {'detail': 'temporarily_unavailable'}),
])
def test_unrecognized_or_configuration_rejections_keep_event_for_retry(tmp_path, status, body):
    state = state_at(tmp_path)
    saved = {'marker': 42, 'pending': [{'text': 'keep'}], 'next_marker': 43}
    state.save(saved)
    def local(request):
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status, text='not JSON')
    with httpx.Client() as api, httpx.Client(transport=httpx.MockTransport(local)) as receiver:
        with pytest.raises(PollingError, match=f'local_webhook_http_{status}'):
            step(api, receiver, state)
    assert state_at(tmp_path).read() == saved


def test_rejection_must_be_saved_before_cursor_or_later_delivery(tmp_path, monkeypatch):
    state = state_at(tmp_path)
    saved = {'marker': 42, 'pending': [{'text': 'invalid'}, {'text': 'next'}], 'next_marker': 43}
    state.save(saved)
    def replace_failure(*args, **kwargs):
        raise OSError('fixture disk failure')
    monkeypatch.setattr(Path, 'replace', replace_failure)
    def local(request):
        assert json.loads(request.content) == saved['pending'][0]
        return httpx.Response(400, json={'detail': 'invalid_update'})
    with httpx.Client() as api, httpx.Client(transport=httpx.MockTransport(local)) as receiver:
        with pytest.raises(OSError, match='fixture disk failure'):
            step(api, receiver, state)
    assert state_at(tmp_path).read() == saved


def test_all_rejected_updates_commit_disposition_and_cursor_together(tmp_path, monkeypatch):
    state = state_at(tmp_path)
    state.save({'marker': 42, 'pending': [{'text': 'invalid'}], 'next_marker': 43})
    save = state.save
    def crash_after_save(value):
        save(value)
        raise OSError('fixture process interrupted after durable save')
    monkeypatch.setattr(state, 'save', crash_after_save)
    with httpx.Client() as api, httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(400, json={'detail': 'invalid_update'}))) as receiver:
        with pytest.raises(OSError, match='fixture process interrupted'):
            step(api, receiver, state)
    assert state_at(tmp_path).read() == {'marker': 43, 'pending': [], 'next_marker': None,
                                        'rejected_counts': {'invalid_update': 1}}


@pytest.mark.parametrize('persistent_failure', [False, True])
def test_failed_directory_sync_cannot_acknowledge_cursor_after_restart(tmp_path, monkeypatch,
                                                                      persistent_failure):
    state = state_at(tmp_path)
    state.save({'marker': 42, 'pending': [{'text': 'invalid'}], 'next_marker': 43})
    replace, fsync = Path.replace, os.fsync
    renamed, sync_failed, synced_after_failure = False, False, False

    def checkpoint_replace(path, target):
        nonlocal renamed
        result = replace(path, target)
        renamed = True
        return result

    def checkpoint_sync(descriptor):
        nonlocal sync_failed, synced_after_failure
        if stat.S_ISDIR(os.fstat(descriptor).st_mode) and renamed:
            if not sync_failed or persistent_failure:
                sync_failed = True
                raise OSError('fixture directory sync unavailable')
            fsync(descriptor)
            synced_after_failure = True
        else:
            fsync(descriptor)

    monkeypatch.setattr(Path, 'replace', checkpoint_replace)
    monkeypatch.setattr(os, 'fsync', checkpoint_sync)
    with httpx.Client() as api, httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(400, json={'detail': 'invalid_update'}))) as receiver:
        with pytest.raises(OSError, match='fixture directory sync unavailable'):
            step(api, receiver, state)

    def remote(request):
        assert synced_after_failure, 'Cursor acknowledged before checkpoint directory was synchronized'
        assert request.url.params['marker'] == '43'
        return httpx.Response(200, json={'updates': [], 'marker': None})

    with httpx.Client(transport=httpx.MockTransport(remote)) as api, httpx.Client() as receiver:
        if persistent_failure:
            with pytest.raises(OSError, match='fixture directory sync unavailable'):
                step(api, receiver, state_at(tmp_path))
        else:
            assert step(api, receiver, state_at(tmp_path)) == 0
            assert state.read()['rejected_counts'] == {'invalid_update': 1}


# The regression also crosses the real HTTP parser and PostgreSQL inbox.
from test_application import app


def test_too_long_message_does_not_block_valid_message_in_real_inbox(app, tmp_path):
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from tsr.http import create_app
    settings = SimpleNamespace(max_webhook_secret='fixture-secret', readiness_secret='ready',
        identity_hmac_key='fixture-identity', bot_scope='fixture', max_body_bytes=131072, max_input_chars=2000)
    original = json.loads((Path(__file__).parent / 'fixtures/max/text.json').read_text())
    long_message = json.loads(json.dumps(original))
    long_message['message']['body'].update(mid='long', text='A' * 2001)
    valid_message = json.loads(json.dumps(original))
    valid_message['message']['body'].update(mid='next', text='70')
    with TestClient(create_app(settings, app.db)) as endpoint:
        def local(request):
            response = endpoint.post('/webhooks/max', content=request.content, headers=dict(request.headers))
            return httpx.Response(response.status_code, content=response.content)
        state = state_at(tmp_path)
        state.save({'marker': 42, 'pending': [long_message, valid_message], 'next_marker': 43})
        with httpx.Client() as api, httpx.Client(transport=httpx.MockTransport(local)) as receiver:
            assert step(api, receiver, state) == 1
    assert state_at(tmp_path).read() == {'marker': 43, 'pending': [], 'next_marker': None,
                                        'rejected_counts': {'invalid_update': 1}}
    with app.db.uow() as uow:
        records = uow.connection.execute('SELECT id FROM inbox_events').fetchall()
        assert len(records) == 1
        assert uow.inbox.get(records[0]['id']).event.payload.text == '70'
        assert uow.connection.execute('SELECT count(*) AS n FROM jobs').fetchone()['n'] == 1
