"""Local bridge reliability: never acknowledge an event before durable ingress."""
import json

import httpx
import pytest

from tools.poll_max import PollingError, State, api_json, poll_once, require_no_subscription


def state_at(tmp_path, identity='fixture-bot'):
    return State(tmp_path / 'polling.state', b'p' * 32, identity)


def step(api, local, state):
    return poll_once(api, local, base='https://platform-api2.max.ru', token='fixture-token',
                     endpoint='http://app:8080/webhooks/max', secret='fixture-secret', state=state)


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
