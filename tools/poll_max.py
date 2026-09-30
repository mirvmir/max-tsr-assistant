"""Local MAX polling bridge; use the HTTPS webhook deployment for production.

Responses are encrypted before forwarding to the ordinary authenticated HTTP
inbox. A cursor advances only after every event has been durably accepted or
explicitly rejected by the local input validator. Rejections retain only counts
and fixed reasons, never message contents.
Restarting replays any pending batch; the application's inbox deduplicates it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import ssl
import threading
import time
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_PERMANENT_REJECTIONS = {400: 'invalid_update', 413: 'body_too_large'}


class PollingError(Exception):
    """Only fixed diagnostic codes, never response bodies or credentials."""


def api_json(client, base, path, token, *, params=None):
    with client.stream('GET', base + path, params=params,
                       headers={'Authorization': token}) as response:
        if response.status_code != 200:
            raise PollingError(f'max_http_{response.status_code}')
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > 5_242_880:
                raise PollingError('max_response_too_large')
    try:
        value = json.loads(body)
    except ValueError:
        raise PollingError('max_invalid_json') from None
    if not isinstance(value, dict):
        raise PollingError('max_invalid_response')
    return value


def require_no_subscription(response):
    subscriptions = response.get('subscriptions')
    if not isinstance(subscriptions, list):
        raise PollingError('max_invalid_subscriptions')
    if subscriptions:
        raise PollingError('webhook_already_registered')


class State:
    def __init__(self, path, key, identity):
        self.path = Path(path)
        self.cipher = AESGCM(key)
        self.identity = identity.encode('utf-8')

    def read(self):
        if not self.path.exists():
            return {'marker': None, 'pending': [], 'next_marker': None}
        # A prior save may have replaced the file but failed its directory sync.
        # Confirm that checkpoint before its cursor can acknowledge remote events.
        self._sync_directory()
        try:
            content = self.path.read_bytes()
            result = json.loads(self.cipher.decrypt(content[:12], content[12:], self.identity))
            if not isinstance(result, dict) or not isinstance(result.get('pending'), list):
                raise ValueError('invalid_state')
            for key in ('marker', 'next_marker'):
                if result.get(key) is not None and type(result[key]) is not int:
                    raise ValueError('invalid_marker')
            counts = result.get('rejected_counts', {})
            if not isinstance(counts, dict) or any(
                    reason not in _PERMANENT_REJECTIONS.values() or type(count) is not int or count < 0
                    for reason, count in counts.items()):
                raise ValueError('invalid_rejected_counts')
            return result
        except Exception:
            raise PollingError('polling_state_invalid') from None

    def save(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        nonce = os.urandom(12)
        content = nonce + self.cipher.encrypt(nonce, json.dumps(value).encode(), self.identity)
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('wb') as stream:
            os.chmod(temporary, 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)
        self._sync_directory()

    def _sync_directory(self):
        if os.name == 'posix':
            # Persist the rename before a later request acknowledges the cursor.
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)


def poll_once(api, local, *, base, token, endpoint, secret, state, max_body_bytes=131072):
    current = state.read()
    if not current['pending']:
        params = {'limit': 20, 'timeout': 30,
                  'types': 'bot_started,message_created,message_callback'}
        if current['marker'] is not None:
            params['marker'] = current['marker']
        batch = api_json(api, base, '/updates', token, params=params)
        updates, marker = batch.get('updates'), batch.get('marker')
        if not isinstance(updates, list) or len(updates) > 20 or (marker is not None and type(marker) is not int):
            raise PollingError('max_invalid_updates')
        if updates and marker is None:
            raise PollingError('max_missing_marker')
        # Keep the response across a local outage or restart, including the first
        # response where MAX was queried without a previous marker.
        current.update(pending=updates,
                       next_marker=marker if marker is not None else current['marker'])
        state.save(current)
    count, index = 0, 0
    while index < len(current['pending']):
        update = current['pending'][index]
        body = json.dumps(update, ensure_ascii=False).encode('utf-8')
        rejection = None
        if len(body) > max_body_bytes:
            rejection = 'body_too_large'
        else:
            response = local.post(endpoint, content=body, headers={
                'X-Max-Bot-Api-Secret': secret, 'Content-Type': 'application/json'})
            if response.status_code != 200:
                reason = _PERMANENT_REJECTIONS.get(response.status_code)
                try:
                    if reason and response.json() == {'detail': reason}:
                        rejection = reason
                except ValueError:
                    pass
                if rejection is None:
                    raise PollingError(f'local_webhook_http_{response.status_code}')
            else:
                try:
                    accepted = response.json() == {'status': 'accepted'}
                except ValueError:
                    accepted = False
                if not accepted:
                    raise PollingError('local_webhook_invalid_ack')
        if rejection:
            current['pending'].pop(index)
            counts = current.setdefault('rejected_counts', {})
            counts[rejection] = counts.get(rejection, 0) + 1
            if not current['pending']:
                # No empty pending batch with an uncommitted next cursor may
                # survive a restart, especially the first request without one.
                current.update(marker=current['next_marker'], next_marker=None)
            state.save(current)
            print('Polling rejected update: ' + rejection, flush=True)
            if not current['pending']:
                return count
            continue
        count += 1
        index += 1
    state.save(dict(current, marker=current['next_marker'], pending=[], next_marker=None))
    return count


def run(stop):
    from tsr.config import load_settings
    result = load_settings()
    if not result.ok:
        raise PollingError('configuration_invalid')
    settings = result.value
    if settings.mode != 'demo' or settings.webhook_url:
        raise PollingError('polling_requires_local_demo_without_webhook')
    token = settings.max_token.get_secret_value() if settings.max_token else ''
    secret = settings.max_webhook_secret.get_secret_value() if settings.max_webhook_secret else ''
    if not token or not secret:
        raise PollingError('max_token_or_webhook_secret_missing')
    base = settings.max_api_url.rstrip('/')
    address = urlsplit(base)
    if address.scheme != 'https' or not address.netloc or address.username or address.password or address.query or address.fragment:
        raise PollingError('invalid_max_url')
    endpoint = os.environ.get('TSR_LOCAL_WEBHOOK_URL', 'http://127.0.0.1:8080/webhooks/max')
    target = urlsplit(endpoint)
    if target.scheme != 'http' or target.hostname not in {'app', 'localhost', '127.0.0.1'} or target.path != '/webhooks/max' or target.username or target.password or target.query or target.fragment:
        raise PollingError('local_webhook_required')
    context = ssl.create_default_context(cafile=str(settings.max_ca_bundle) if settings.max_ca_bundle else None)
    with httpx.Client(verify=context, timeout=45, follow_redirects=False, trust_env=False) as api, \
         httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as local:
        bot = api_json(api, base, '/me', token)
        if bot.get('is_bot') is not True or type(bot.get('user_id')) is not int:
            raise PollingError('max_invalid_bot_identity')
        require_no_subscription(api_json(api, base, '/subscriptions', token))
        identity = json.dumps(['max-local-polling-v1', base, settings.bot_scope, bot['user_id']])
        state = State(os.environ.get('TSR_POLLING_STATE', 'var/polling/polling.state'),
                      settings.encryption_key_bytes, identity)
        state.path.parent.mkdir(parents=True, exist_ok=True)
        # The Compose bridge runs on Linux. Lock the shared state for its lifetime
        # so accidental duplicate containers cannot advance the same cursor.
        import fcntl
        with state.path.with_suffix('.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PollingError('polling_already_running') from None
            state.read()
            print('MAX local polling connected; waiting for a private bot conversation.', flush=True)
            checked = time.monotonic()
            while not stop.is_set():
                try:
                    if time.monotonic() - checked >= 300:
                        require_no_subscription(api_json(api, base, '/subscriptions', token))
                        checked = time.monotonic()
                    count = poll_once(api, local, base=base, token=token, endpoint=endpoint,
                                      secret=secret, state=state, max_body_bytes=settings.max_body_bytes)
                    if count:
                        print(f'Durably accepted updates: {count}', flush=True)
                    stop.wait(1)
                except (httpx.HTTPError, PollingError, OSError) as exc:
                    code = str(exc) if isinstance(exc, PollingError) else type(exc).__name__
                    print('Polling delayed: ' + code, flush=True)
                    if code in {'webhook_already_registered', 'max_http_401', 'max_http_403',
                                'max_http_405', 'polling_state_invalid'}:
                        return 1
                    stop.wait(5)
    return 0


def main():
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        return run(stop)
    except (httpx.HTTPError, PollingError, OSError) as exc:
        code = str(exc) if isinstance(exc, PollingError) else type(exc).__name__
        print('Polling startup failed: ' + code, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
