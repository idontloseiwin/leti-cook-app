"""Pure Reconnect All authorization model; no SQL, provider calls or scheduler.

Snapshot tokens are optimistic concurrency markers, never auth/capabilities.
The caller must supply the full pending set from one bounded DB snapshot and
repeat this check under the existing restore transaction lock before enqueue.
"""
from dataclasses import dataclass
import hashlib
import json
import re

from saved_work import SavedWorkError, revision
from user_work_backup import LIMITS, UserWorkError, rows, uid

_TOKEN = re.compile(r'reconnect-v1:[0-9a-f]{64}')


def _facts(items):
    facts = []
    seen = set()
    for item in rows(items, LIMITS['channels']):
        if type(item) is not dict or type(item.get('is_hidden')) is not bool:
            raise UserWorkError('invalid_user_work_request')
        try:
            identity, version = uid(item['id']), revision(item['revision'])
        except (KeyError, SavedWorkError):
            raise UserWorkError('invalid_user_work_request') from None
        if identity in seen:
            raise UserWorkError('invalid_user_work_request')
        seen.add(identity)
        facts.append((identity, version, item['is_hidden']))
    return sorted(facts)


def reconnect_snapshot(items):
    """No links, organization or provider data enter the token."""
    encoded = json.dumps(_facts(items), separators=(',', ':'), ensure_ascii=True).encode('ascii')
    return 'reconnect-v1:' + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ReconnectAuthorization:
    snapshot: str
    # Each pair authorizes exactly one pending user-work revision.
    items: tuple[tuple[str, int], ...]
    excluded_hidden: int


def authorize_reconnect(items, body):
    """Pure set selection; no mutation, retry reset, lease or provider action."""
    if (type(body) is not dict or set(body) != {'expected_snapshot'}
            or type(body['expected_snapshot']) is not str or not _TOKEN.fullmatch(body['expected_snapshot'])):
        raise UserWorkError('invalid_user_work_request')
    facts = _facts(items)
    actual = reconnect_snapshot(items)
    if actual != body['expected_snapshot']:
        raise UserWorkError('conflict', 409)
    return ReconnectAuthorization(actual, tuple((identity, version) for identity, version, hidden in facts if not hidden),
                                  sum(hidden for _, _, hidden in facts))
