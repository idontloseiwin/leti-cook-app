"""YouTube WebSub (PubSubHubbub) primitives: capabilities, signatures, parsing, Hub requests.

No database or collector state lives here. A notification is an untrusted hint
that existing work should run sooner; it never becomes video data by itself.

Topic: YouTube pings the Hub only for /xml/feeds/videos.xml, and the Hub matches
the topic string exactly (https://issuetracker.google.com/issues/566069563).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HUB_URL = 'https://pubsubhubbub.appspot.com/subscribe'
TOPIC_PREFIX = 'https://www.youtube.com/xml/feeds/videos.xml?channel_id='
LEASE_SECONDS = 864_000
HUB_TIMEOUT_SECONDS = 120
MAX_NOTIFICATION_BYTES = 262_144
MAX_EVENTS_PER_NOTIFICATION = 100
KEY_BYTES = 32

CHANNEL_ID_RE = re.compile(r'UC[A-Za-z0-9_-]{22}')
VIDEO_ID_RE = re.compile(r'[A-Za-z0-9_-]{11}')
TOKEN_RE = re.compile(r'[A-Za-z0-9_-]{43}')
PATH_RE = re.compile(r'/websub/(UC[A-Za-z0-9_-]{22})/([A-Za-z0-9_-]{43})')
_ATOM = 'http://www.w3.org/2005/Atom'
_YT = 'http://www.youtube.com/xml/schemas/2015'
_AT = 'http://purl.org/atompub/tombstones/1.0'


class WebSubError(ValueError):
    """A fixed code without provider text."""


# ------------------------------------------------------------------ configuration

def callback_base(value):
    """Public HTTPS base of this installation's /websub/ path; empty or None keeps WebSub off."""
    value = (value or '').strip()
    if not value:
        return None
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or '.' not in parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or value.endswith('/')
            or len(value) > 200 or not value.isascii() or any(c.isspace() for c in value)):
        raise WebSubError('websub_callback_url_invalid')
    return value


def public_host(base):
    """The Host header a reverse proxy must forward for callbacks (netloc of the base)."""
    return urllib.parse.urlsplit(base).netloc.lower()


# ------------------------------------------------------------------ capabilities

def load_or_create_key(path):
    """One random installation key in the private state volume; created once, never logged."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'wb') as out:
            out.write(secrets.token_bytes(KEY_BYTES))
            out.flush()
            os.fsync(out.fileno())
    data = path.read_bytes()
    if len(data) != KEY_BYTES:
        raise WebSubError('websub_key_invalid')
    return data


def _derive(key, purpose, channel_id, generation):
    message = f'{purpose}\n{channel_id}\n{int(generation)}'.encode('ascii')
    return hmac.new(key, message, hashlib.sha256).digest()


def capability_token(key, channel_id, generation):
    return base64.urlsafe_b64encode(_derive(key, 'callback', channel_id, generation)).decode('ascii').rstrip('=')


def signing_secret(key, channel_id, generation):
    return _derive(key, 'secret', channel_id, generation).hex()


def callback_url(base, key, channel_id, generation):
    return f'{base}/websub/{channel_id}/{capability_token(key, channel_id, generation)}'


def topic_url(channel_id):
    if not CHANNEL_ID_RE.fullmatch(str(channel_id or '')):
        raise WebSubError('websub_channel_invalid')
    return TOPIC_PREFIX + channel_id


def parse_path(path):
    """(channel_id, token) for an exact callback path, else None."""
    match = PATH_RE.fullmatch(path or '')
    return (match.group(1), match.group(2)) if match else None


def token_matches(key, channel_id, generation, token):
    return hmac.compare_digest(capability_token(key, channel_id, generation), token)


def verify_signature(body, header, secret):
    """X-Hub-Signature: sha1=... (the Hub's algorithm) or sha256=..., constant time."""
    if not body or not header or not secret:
        return False
    for part in header.split(','):
        algorithm, _, supplied = part.strip().partition('=')
        algorithm = algorithm.lower()
        if algorithm in ('sha1', 'sha256') and supplied:
            digest = hmac.new(secret.encode('ascii'), body, getattr(hashlib, algorithm)).hexdigest()
            if hmac.compare_digest(digest, supplied.strip().lower()):
                return True
    return False


# ------------------------------------------------------------------ notifications

@dataclass(frozen=True)
class Notification:
    channel_id: str
    video_id: str
    kind: str              # 'entry' (new or updated video) or 'deleted'
    published_at: datetime | None
    updated_at: datetime | None
    dedupe_key: str


class _NoDoctype(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        # Runs after encoding detection and before any DTD or entity is accepted.
        raise WebSubError('websub_xml_forbidden')


def _text(element, path):
    child = element.find(path)
    value = str(child.text or '').strip() if child is not None else ''
    return value or None


def _timestamp(value):
    if not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _topic_channel(href):
    parsed = urllib.parse.urlparse(href or '')
    if parsed.scheme != 'https' or parsed.netloc not in ('www.youtube.com', 'youtube.com'):
        return None
    if parsed.path not in ('/feeds/videos.xml', '/xml/feeds/videos.xml'):
        return None
    value = (urllib.parse.parse_qs(parsed.query).get('channel_id') or [None])[0]
    return value if value and CHANNEL_ID_RE.fullmatch(value) else None


def _dedupe(channel_id, video_id, kind, published, updated):
    raw = '\n'.join((channel_id, video_id, kind, published or '', updated or ''))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def parse_notification(body):
    if not body or len(body) > MAX_NOTIFICATION_BYTES:
        raise WebSubError('websub_size_invalid')
    try:
        root = ET.fromstring(body, parser=ET.XMLParser(target=_NoDoctype()))
    except WebSubError:
        raise
    except (ET.ParseError, ValueError, LookupError):
        raise WebSubError('websub_xml_invalid') from None
    if root.tag != f'{{{_ATOM}}}feed':
        raise WebSubError('websub_root_invalid')
    feed_channel = next((c for c in (_topic_channel(link.attrib.get('href')) for link in root.findall(f'{{{_ATOM}}}link')
                                     if link.attrib.get('rel') == 'self') if c), None)
    out = []
    for entry in root.findall(f'{{{_ATOM}}}entry'):
        video = _text(entry, f'{{{_YT}}}videoId')
        channel = _text(entry, f'{{{_YT}}}channelId') or feed_channel
        if not (video and VIDEO_ID_RE.fullmatch(video) and channel and CHANNEL_ID_RE.fullmatch(channel)):
            raise WebSubError('websub_entry_invalid')
        published, updated = _text(entry, f'{{{_ATOM}}}published'), _text(entry, f'{{{_ATOM}}}updated')
        out.append(Notification(channel, video, 'entry', _timestamp(published), _timestamp(updated),
                                _dedupe(channel, video, 'entry', published, updated)))
    for deleted in root.findall(f'{{{_AT}}}deleted-entry'):
        ref = str(deleted.attrib.get('ref') or '')
        video = ref.removeprefix('yt:video:') if ref.startswith('yt:video:') else None
        channel = feed_channel
        by = deleted.find(f'{{{_AT}}}by')
        if not channel and by is not None:
            uri = _text(by, f'{{{_ATOM}}}uri') or _text(by, f'{{{_AT}}}uri') or _text(by, 'uri') or ''
            parsed = urllib.parse.urlparse(uri)
            parts = [p for p in parsed.path.split('/') if p]
            if parsed.netloc in ('www.youtube.com', 'youtube.com') and len(parts) == 2 and parts[0] == 'channel':
                channel = parts[1]
        if not (video and VIDEO_ID_RE.fullmatch(video) and channel and CHANNEL_ID_RE.fullmatch(channel)):
            raise WebSubError('websub_deleted_invalid')
        when = str(deleted.attrib.get('when') or '').strip() or None
        out.append(Notification(channel, video, 'deleted', None, _timestamp(when),
                                _dedupe(channel, video, 'deleted', None, when)))
    if not out:
        raise WebSubError('websub_feed_empty')
    if len(out) > MAX_EVENTS_PER_NOTIFICATION:
        raise WebSubError('websub_feed_too_large')
    return out


# ------------------------------------------------------------------ Hub requests

@dataclass(frozen=True)
class HubResult:
    outcome: str            # accepted | http_<status> | timeout | network
    http_status: int | None
    retry_after: int | None
    seconds: float

    @property
    def accepted(self):
        return self.outcome == 'accepted'

    @property
    def ambiguous(self):
        """No answer: the Hub may still have the request, so it must not be resent at once."""
        return self.http_status is None


def _retry_after(value):
    value = (value or '').strip()
    if value.isdigit():
        return min(int(value), 86400)
    try:
        when = datetime.strptime(value, '%a, %d %b %Y %H:%M:%S GMT').replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0, min(86400, int(when.timestamp() - time.time())))


def hub_request(mode, channel_id, callback, *, secret=None, lease_seconds=LEASE_SECONDS,
                hub=HUB_URL, timeout=HUB_TIMEOUT_SECONDS, opener=urllib.request.urlopen, clock=time.monotonic):
    """One subscribe or unsubscribe POST. Waits for the answer up to `timeout`; never retries."""
    if mode not in ('subscribe', 'unsubscribe'):
        raise WebSubError('websub_mode_invalid')
    form = {'hub.mode': mode, 'hub.topic': topic_url(channel_id), 'hub.callback': callback, 'hub.verify': 'async'}
    if mode == 'subscribe':
        form.update({'hub.secret': secret, 'hub.lease_seconds': str(int(lease_seconds))})
    request = urllib.request.Request(hub, data=urllib.parse.urlencode(form).encode('ascii'), method='POST',
                                     headers={'Content-Type': 'application/x-www-form-urlencoded'})
    started = clock()
    try:
        with opener(request, timeout=timeout) as response:
            status = response.status
            return HubResult('accepted' if 200 <= status < 300 else f'http_{status}', status, None, clock() - started)
    except urllib.error.HTTPError as error:
        return HubResult(f'http_{error.code}', error.code, _retry_after(error.headers.get('Retry-After')), clock() - started)
    except (TimeoutError, urllib.error.URLError, OSError) as error:
        reason = getattr(error, 'reason', error)
        timed_out = isinstance(reason, TimeoutError) or 'timed out' in str(reason).lower()
        return HubResult('timeout' if timed_out else 'network', None, None, clock() - started)
