"""Public WebSub callback ingress: the only route that accepts a non-local Host.

Outermost ASGI layer, outside owner access and the HTTP read scope. With
WebSub configured, a request whose Host is the public callback host reaches
nothing but the exact /websub/<channel>/<capability> handlers; every other path
on that host is 404 before the application sees it. /websub/ on any other Host
is 404 as well. The verification GET writes only its own outcome: the Hub's
protocol confirms subscriptions with GET.
"""
import urllib.parse

import anyio

import websub

MAX_CHALLENGE = 200
WORKERS = 5
ADMISSION = 10


def _wake_collection():
    from operator_jobs import signal_operator_job_worker
    signal_operator_job_worker(discovery=True)


class WebSubIngressMiddleware:
    def __init__(self, app, *, base, key, repository, wake=_wake_collection):
        """base: configured callback base or None; key: callable returning the installation key."""
        self.app, self.base, self.key, self.repository = app, base, key, repository
        self.wake = wake
        self.host = websub.public_host(base) if base else None
        self._limiter = anyio.CapacityLimiter(WORKERS)
        self._admitted = 0

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        path = scope.get('path') or ''
        host = next((v.decode('latin-1').lower() for k, v in scope.get('headers') or () if k == b'host'), None)
        public = self.host is not None and host == self.host
        if not public and not path.startswith('/websub/'):
            return await self.app(scope, receive, send)
        if not public or (target := websub.parse_path(path)) is None:
            return await _reply(send, 404)
        if scope['method'] not in ('GET', 'POST'):
            return await _reply(send, 405)
        if self._admitted >= ADMISSION:
            return await _reply(send, 503)
        self._admitted += 1
        try:
            if scope['method'] == 'GET':
                return await self._verify(scope, send, *target)
            return await self._deliver(scope, receive, send, *target)
        finally:
            self._admitted -= 1

    async def _call(self, function, *args, **kwargs):
        return await anyio.to_thread.run_sync(lambda: function(*args, **kwargs), limiter=self._limiter)

    async def _subscription(self, channel_id, token):
        row = await self._call(self.repository.subscription, channel_id)
        if row is None or not websub.token_matches(self.key(), channel_id, row['generation'], token):
            return None
        return row

    async def _verify(self, scope, send, channel_id, token):
        query = urllib.parse.parse_qs(scope.get('query_string', b'').decode('latin-1'), keep_blank_values=True)
        one = lambda name: (query.get(name) or [None])[0]
        mode, topic, challenge, lease = one('hub.mode'), one('hub.topic'), one('hub.challenge'), one('hub.lease_seconds')
        lease = int(lease) if lease and lease.isdigit() and len(lease) <= 9 else None
        try:
            row = await self._subscription(channel_id, token)
            if row is None:
                return await _reply(send, 404)
            if mode not in ('subscribe', 'unsubscribe', 'denied') or topic != row['topic']:
                await self._call(self.repository.journal, 'verification', 'refused', channel=channel_id,
                                 mode=mode if mode in ('subscribe', 'unsubscribe', 'denied') else None)
                return await _reply(send, 404)
            if mode == 'denied':
                await self._call(self.repository.verification, channel_id, row['generation'], mode, None, outcome='denied')
                return await _reply(send, 200)
            expected = 'unsubscribing' if mode == 'unsubscribe' else ('pending', 'verified')
            if (row['state'] not in expected or not challenge or len(challenge) > MAX_CHALLENGE
                    or not challenge.isprintable()):
                await self._call(self.repository.journal, 'verification', 'refused', channel=channel_id, mode=mode)
                return await _reply(send, 404)
            outcome = await self._call(self.repository.verification, channel_id, row['generation'], mode, lease,
                                       outcome='verified')
            if outcome != 'verified':
                return await _reply(send, 404)
            return await _reply(send, 200, challenge.encode('utf-8'))
        except Exception:
            # Database or runtime unavailable: the Hub retries verification later.
            return await _reply(send, 503)

    async def _deliver(self, scope, receive, send, channel_id, token):
        length = next((v for k, v in scope.get('headers') or () if k == b'content-length'), None)
        if length is None or not length.isdigit():
            return await _reply(send, 411)
        if int(length) > websub.MAX_NOTIFICATION_BYTES:
            return await _reply(send, 413)
        body = await _read(receive, int(length))
        if body is None:
            return await _reply(send, 400)
        signature = next((v.decode('latin-1') for k, v in scope.get('headers') or () if k == b'x-hub-signature'), None)
        try:
            row = await self._subscription(channel_id, token)
            if row is None or row['state'] not in ('verified', 'unsubscribing'):
                return await _reply(send, 404)
            secret = websub.signing_secret(self.key(), channel_id, row['generation'])
            if not websub.verify_signature(body, signature, secret):
                # Discarded before parsing; the Hub must not retry a forgery.
                await self._call(self.repository.journal, 'delivery', 'bad_signature', channel=channel_id, size=len(body))
                return await _reply(send, 204)
            try:
                notifications = websub.parse_notification(body)
            except websub.WebSubError as error:
                await self._call(self.repository.journal, 'delivery', str(error).removeprefix('websub_'),
                                 channel=channel_id, size=len(body))
                return await _reply(send, 204)
            own = [n for n in notifications if n.channel_id == channel_id]
            outcome = 'accepted' if len(own) == len(notifications) else 'channel_mismatch'
            await self._call(self.repository.delivery, channel_id, own, size=len(body), outcome=outcome)
            if own:
                try:
                    # Delivery has committed. A lost hint must not ask the Hub
                    # to replay a durable notification; the idle poll recovers it.
                    await self._call(self.wake)
                except Exception:
                    pass
            return await _reply(send, 204)
        except Exception:
            # Not stored: the Hub retries the delivery.
            return await _reply(send, 503)


async def _read(receive, length):
    chunks, size = [], 0
    while True:
        message = await receive()
        if message['type'] != 'http.request':
            return None
        chunk = message.get('body', b'')
        size += len(chunk)
        if size > length:
            return None
        chunks.append(chunk)
        if not message.get('more_body', False):
            break
    return b''.join(chunks) if size == length else None


async def _reply(send, status, body=b''):
    await send({'type': 'http.response.start', 'status': status, 'headers': [
        (b'content-type', b'text/plain; charset=utf-8'), (b'content-length', str(len(body)).encode('ascii')),
        (b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff')]})
    await send({'type': 'http.response.body', 'body': body})
