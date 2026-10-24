"""Strict user-work-only backup format. No database, provider, or source DTO I/O."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from saved_work import normalize_conditions, saved_id, saved_name, revision, SavedWorkError
from user_channel_input import normalize_user_channel_input
from hot_storage import utc_instant

FORMAT = 'leti-cook-user-work'
# Read existing user exports; every normalized/new backup uses the current name.
READ_FORMATS = (FORMAT, 'let-it-cook-user-work')
# Format 2 lists the preset Monetized label when it is kept, under its own name;
# format 1 files, from before it could be renamed or deleted, imply it unchanged.
VERSION = 2
READ_VERSIONS = (1, 2)
MAX_BYTES = 16 * 1024 * 1024
LIMITS = {'folders': 1000, 'labels': 1000, 'channels': 10000, 'collections': 1000,
          'reviews': 1000, 'views': 1000, 'members': 100000, 'history': 10000, 'assignments': 100000}
SAVED_FIELDS = ('id', 'name', 'revision', 'created_at', 'updated_at')
REVIEW_FIELDS = ('case_id', 'revision', 'state', 'decision_type', 'decision_reason',
                 'decision_at', 'decision_revision', 'history')
HISTORY_FIELDS = ('revision', 'video_type', 'reason', 'decided_at', 'request_id', 'expected_revision')
CHANNEL_FIELDS = ('id', 'user_input', 'folder_id', 'parent_ref', 'is_hidden', 'added_at', 'label_keys', 'saved_views')
# A channel in the trash also carries its original removal time, so a restore
# does not restart the trash window. Older backups omit it.
HIDDEN_CHANNEL_FIELDS = CHANNEL_FIELDS + ('hidden_at',)
# A saved video or manual decision names its channel, so removing that channel
# for good also removes them after a restore. Older backups omit it.
MEMBER_FIELDS = ('video_id', 'added_at')
OWNED_MEMBER_FIELDS = MEMBER_FIELDS + ('channel_ref',)


class UserWorkError(Exception):
    def __init__(self, code='invalid_user_work_backup', status=422):
        self.code, self.status = code, status
        super().__init__(code)


def exact(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise UserWorkError()
    return value


def rows(value, maximum):
    if type(value) is not list or len(value) > maximum:
        raise UserWorkError('user_work_limit_exceeded')
    return value


def text(value, maximum=120, *, blank=False):
    if not isinstance(value, str) or len(value) > maximum or (not blank and not value.strip()):
        raise UserWorkError()
    if any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise UserWorkError()
    return value


def stamp(value, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise UserWorkError()
    try:
        parsed = utc_instant(value)
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        raise UserWorkError() from None


def uid(value, *, nullable=False):
    if value is None and nullable:
        return None
    return saved_id(value)


def owner(result, row):
    if row.get('channel_ref') is not None:
        result['channel_ref'] = uid(row['channel_ref'])
    return result


def hidden_time(value, is_hidden):
    if value is None:
        return None
    if not is_hidden:
        raise UserWorkError()
    value = stamp(value)
    if utc_instant(value) > datetime.now(timezone.utc):
        raise UserWorkError()
    return value


def label_key(value):
    if value == 'monetized':
        return value
    if not isinstance(value, str) or not value.startswith('custom:'):
        raise UserWorkError()
    return 'custom:' + uid(value[7:])


def unique(values):
    if len(values) != len(set(values)):
        raise UserWorkError('duplicate_user_work_identity')


def preferences(value, channel_ids, folder_ids):
    exact(value, ('catalogue_density', 'bookmarked_channel_refs', 'collapsed_folder_ids'))
    if value['catalogue_density'] not in ('large', 'compact'):
        raise UserWorkError()
    refs = [uid(v) for v in rows(value['bookmarked_channel_refs'], LIMITS['channels'])]
    folders = [v if v == 'unassigned' else uid(v) for v in rows(value['collapsed_folder_ids'], 500)]
    unique(refs)
    unique(folders)
    if not set(refs) <= channel_ids or not set(folders) <= folder_ids | {'unassigned'}:
        raise UserWorkError('missing_user_work_reference')
    return {'catalogue_density': value['catalogue_density'], 'bookmarked_channel_refs': refs,
            'collapsed_folder_ids': folders}


def _saved(row, kind):
    extra = ('conditions',) if kind == 'view' else ('members',)
    exact(row, SAVED_FIELDS + extra)
    result = {'id': uid(row['id']), 'name': saved_name(row['name']), 'revision': revision(row['revision']),
              'created_at': stamp(row['created_at']), 'updated_at': stamp(row['updated_at'])}
    if kind == 'view':
        result['conditions'] = normalize_conditions(row['conditions'])
    else:
        members = []
        for member in rows(row['members'], LIMITS['members']):
            exact(member, OWNED_MEMBER_FIELDS if type(member) is dict and 'channel_ref' in member else MEMBER_FIELDS)
            if not isinstance(member['video_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', member['video_id']):
                raise UserWorkError()
            members.append({'video_id': member['video_id'], 'added_at': stamp(member['added_at'])})
            owner(members[-1], member)
        unique([r['video_id'] for r in members])
        result['members'] = members
    return result


def _review(row):
    exact(row, REVIEW_FIELDS + ('channel_ref',) if type(row) is dict and 'channel_ref' in row else REVIEW_FIELDS)
    if row['state'] not in ('open', 'resolved') or row['decision_type'] not in (None, 'long', 'short', 'stream'):
        raise UserWorkError()
    history = []
    for action in rows(row['history'], LIMITS['history']):
        exact(action, HISTORY_FIELDS)
        if action['video_type'] not in ('long', 'short', 'stream'):
            raise UserWorkError()
        expected = action['expected_revision']
        if type(expected) is not int or expected < 1 or action['revision'] != expected + 1:
            raise UserWorkError()
        history.append({'revision': revision(action['revision']), 'video_type': action['video_type'],
                        'reason': text(action['reason'], 1000), 'decided_at': stamp(action['decided_at']),
                        'request_id': uid(action['request_id']), 'expected_revision': expected})
    unique([r['revision'] for r in history])
    unique([r['request_id'] for r in history])
    history.sort(key=lambda r: r['revision'])
    if not history:
        raise UserWorkError('review_has_no_user_decision')
    current_revision = revision(row['revision'])
    if type(current_revision) is not int or current_revision < history[-1]['revision']:
        raise UserWorkError()
    decision_revision = revision(row['decision_revision'])
    if (decision_revision != history[-1]['revision'] or row['decision_type'] != history[-1]['video_type']
            or row['decision_reason'] != history[-1]['reason'] or stamp(row['decision_at']) != history[-1]['decided_at']):
        raise UserWorkError('inconsistent_user_decision')
    return owner({'case_id': uid(row['case_id']), 'revision': current_revision, 'state': row['state'],
            'decision_type': row['decision_type'], 'decision_reason': text(row['decision_reason'], 1000),
            'decision_at': stamp(row['decision_at']), 'decision_revision': decision_revision, 'history': history}, row)


def _raw_length(row, key):
    value = row.get(key) if type(row) is dict else None
    return len(value) if type(value) is list else 0


def _check_raw_totals(work):
    """Reject oversized totals from raw list lengths, before any per-item normalization."""
    channels = rows(work['channels'], LIMITS['channels'])
    collections = rows(work['collections'], LIMITS['collections'])
    reviews = rows(work['reviews'], LIMITS['reviews'])
    if (sum(_raw_length(r, 'label_keys') for r in channels) > LIMITS['assignments']
            or sum(_raw_length(r, 'saved_views') for r in channels) + len(collections) > LIMITS['views']
            or sum(_raw_length(r, 'members') for r in collections) > LIMITS['members']
            or sum(_raw_length(r, 'history') for r in reviews) > LIMITS['history']):
        raise UserWorkError('user_work_limit_exceeded')


def _normalize(document):
    exact(document, ('format', 'version', 'created_at', 'user_work', 'browser_preferences'))
    if document['format'] not in READ_FORMATS or type(document['version']) is not int or document['version'] not in READ_VERSIONS:
        raise UserWorkError('unsupported_user_work_backup')
    work = exact(document['user_work'], ('folders', 'labels', 'channels', 'collections', 'reviews'))
    _check_raw_totals(work)
    result = {key: [] for key in work}
    for row in rows(work['folders'], LIMITS['folders']):
        exact(row, ('id', 'name', 'created_at'))
        result['folders'].append({'id': uid(row['id']), 'name': saved_name(row['name']), 'created_at': stamp(row['created_at'])})
    for row in rows(work['labels'], LIMITS['labels'] + 1):
        exact(row, ('label_key', 'name', 'label_type', 'created_at'))
        key = label_key(row['label_key'])
        kind = 'preset' if key == 'monetized' else 'custom'
        if row['label_type'] != kind or (kind == 'preset' and document['version'] == 1):
            raise UserWorkError('preset_label_in_backup')
        # The same rule as creating a label: surrounding space trimmed, inner space collapsed.
        name = re.sub(r'\s+', ' ', text(row['name'], 40).strip())
        result['labels'].append({'label_key': key, 'name': name,
                                 'label_type': kind, 'created_at': stamp(row['created_at'])})
    if document['version'] == 1:
        result['labels'].append({'label_key': 'monetized', 'name': 'Monetized', 'label_type': 'preset',
                                 'created_at': stamp(document['created_at'])})
    if sum(r['label_type'] == 'custom' for r in result['labels']) > LIMITS['labels']:
        raise UserWorkError('user_work_limit_exceeded')
    for row in rows(work['channels'], LIMITS['channels']):
        exact(row, HIDDEN_CHANNEL_FIELDS if type(row) is dict and 'hidden_at' in row else CHANNEL_FIELDS)
        if type(row['is_hidden']) is not bool:
            raise UserWorkError()
        hidden_at = hidden_time(row.get('hidden_at'), row['is_hidden'])
        locator = None if row['user_input'] is None else normalize_user_channel_input(row['user_input'])
        labels = [label_key(k) for k in rows(row['label_keys'], LIMITS['labels'] + 1)]
        unique(labels)
        result['channels'].append({'id': uid(row['id']), 'user_input': locator,
            'folder_id': uid(row['folder_id'], nullable=True), 'parent_ref': uid(row['parent_ref'], nullable=True),
            'is_hidden': row['is_hidden'], 'added_at': stamp(row['added_at'], nullable=True), 'label_keys': labels,
            'saved_views': [_saved(v, 'view') for v in rows(row['saved_views'], LIMITS['views'])]})
        if hidden_at is not None:
            result['channels'][-1]['hidden_at'] = hidden_at
    result['collections'] = [_saved(r, 'collection') for r in rows(work['collections'], LIMITS['collections'])]
    result['reviews'] = [_review(r) for r in rows(work['reviews'], LIMITS['reviews'])]
    folder_ids = {r['id'] for r in result['folders']}
    labels = {r['label_key'] for r in result['labels']}
    channel_ids = {r['id'] for r in result['channels']}
    owned = [m for c in result['collections'] for m in c['members']] + result['reviews']
    if any(row.get('channel_ref') not in channel_ids for row in owned if 'channel_ref' in row):
        raise UserWorkError('missing_user_work_reference')
    for key, field in (('folders', 'id'), ('labels', 'label_key'), ('channels', 'id'), ('collections', 'id'), ('reviews', 'case_id')):
        unique([r[field] for r in result[key]])
    # Case-insensitive like the database index and label creation.
    unique([r['name'].casefold() for r in result['labels']])
    if sum(len(r['label_keys']) for r in result['channels']) > LIMITS['assignments']:
        raise UserWorkError('user_work_limit_exceeded')
    views = [v for r in result['channels'] for v in r['saved_views']]
    if len(views) + len(result['collections']) > LIMITS['views'] or sum(len(r['members']) for r in result['collections']) > LIMITS['members'] or sum(len(r['history']) for r in result['reviews']) > LIMITS['history']:
        raise UserWorkError('user_work_limit_exceeded')
    unique([v['id'] for v in views])
    parents = {}
    for row in result['channels']:
        if (row['folder_id'] is not None and row['folder_id'] not in folder_ids or
                row['parent_ref'] is not None and row['parent_ref'] not in channel_ids or not set(row['label_keys']) <= labels):
            raise UserWorkError('missing_user_work_reference')
        parents[row['id']] = row['parent_ref']
    done = set()
    for item in parents:
        chain = set()
        while item is not None and item not in done:
            if item in chain:
                raise UserWorkError('cyclic_channel_relationship')
            chain.add(item)
            item = parents[item]
        done.update(chain)
    return {'format': FORMAT, 'version': VERSION, 'created_at': stamp(document['created_at']),
            'user_work': result, 'browser_preferences': preferences(document['browser_preferences'], channel_ids, folder_ids)}


def encode_backup(document):
    try:
        result = json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise UserWorkError() from None
    if len(result) > MAX_BYTES:
        raise UserWorkError('user_work_limit_exceeded', 413)
    return result


def normalize_backup(document):
    encode_backup(document)
    try:
        return _normalize(document)
    except (SavedWorkError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise UserWorkError() from None


def decode_backup(data):
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        raise UserWorkError('user_work_limit_exceeded', 413)
    def pairs(values):
        result = dict(values)
        if len(result) != len(values):
            raise UserWorkError('duplicate_json_key')
        return result
    def invalid_constant(value):
        raise UserWorkError()
    try:
        return normalize_backup(json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid_constant))
    except (UnicodeError, ValueError, RecursionError):
        raise UserWorkError() from None


def import_payload(document, *, normalized=False):
    # A caller holding decode_backup's result skips a second full normalization.
    normalized = document if normalized else normalize_backup(document)
    work = normalized['user_work']
    # Copies: a preview returns the document itself and must not gain fingerprints.
    def fingerprinted(row):
        return {**row, 'create_fingerprint': hashlib.sha256(encode_backup(row)).hexdigest()}
    payload = {'schema_version': VERSION, **work,
               'collections': [fingerprinted(r) for r in work['collections']],
               'channels': [{**c, 'saved_views': [fingerprinted(v) for v in c['saved_views']]} for c in work['channels']]}
    # import_user_work_backup measures its argument as PostgreSQL prints jsonb,
    # with a space after every ',' and ':', so a file near 16 MiB can exceed it.
    printed = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(', ', ': ')).encode('utf-8')
    if len(printed) > MAX_BYTES:
        raise UserWorkError('user_work_limit_exceeded', 413)
    return payload


def build_backup(snapshot, browser_preferences=None, *, now=None):
    """Project an internal consistent snapshot; never serialize enriched API rows."""
    now = stamp(now or datetime.now(timezone.utc).isoformat())
    raw_preferences = browser_preferences or {'catalogue_density': 'large', 'bookmarked_channel_ids': [],
                                             'pending_bookmark_refs': [], 'collapsed_folder_ids': []}
    exact(raw_preferences, ('catalogue_density', 'bookmarked_channel_ids', 'pending_bookmark_refs', 'collapsed_folder_ids'))
    channels, mapping = {}, {}
    def placeholder(api_id):
        if api_id not in mapping:
            ref = str(uuid4())
            mapping[api_id] = ref
            channels[ref] = {'id': ref, 'user_input': None, 'folder_id': None, 'parent_ref': None,
                'is_hidden': False, 'added_at': now, 'label_keys': [], 'saved_views': []}
        return mapping[api_id]
    for row in snapshot['channels']:
        ref = row['user_work_id']
        mapping[row['channel_id']] = ref
        channels[ref] = {'id': ref, 'user_input': row['user_input'], 'folder_id': row['folder_id'],
            'parent_ref': None, 'is_hidden': bool(row['is_hidden']), 'added_at': row['added_at'],
            'label_keys': [], 'saved_views': []}
        if row['is_hidden'] and row['hidden_at'] is not None:
            channels[ref]['hidden_at'] = row['hidden_at']
    for row in snapshot['channels']:
        if row['parent_channel_id'] is not None:
            channels[row['user_work_id']]['parent_ref'] = placeholder(row['parent_channel_id'])
    for row in snapshot['assignments']:
        channels[placeholder(row['channel_id'])]['label_keys'].append(row['label_key'])
    for row in snapshot['pending']:
        if row['id'] in channels:
            raise UserWorkError('duplicate_user_work_identity', 503)
        channels[row['id']] = {k: row[k] for k in CHANNEL_FIELDS if k != 'saved_views'}
        if row['is_hidden'] and row.get('hidden_at') is not None:
            channels[row['id']]['hidden_at'] = row['hidden_at']
        channels[row['id']]['saved_views'] = [{k: v[k] for k in SAVED_FIELDS + ('conditions',)} for v in row['saved_views']]
    member_map = {}
    for member in snapshot['members']:
        member_map.setdefault(member['collection_id'], []).append(owner({k: member[k] for k in MEMBER_FIELDS}, member))
    collections = []
    for row in snapshot['saved_objects']:
        saved = {k: row[k] for k in SAVED_FIELDS}
        if row['kind'] == 'view':
            saved['conditions'] = row['conditions']
            channels[placeholder(row['channel_id'])]['saved_views'].append(saved)
        elif row['kind'] == 'collection':
            saved['members'] = member_map.get(row['id'], [])
            collections.append(saved)
        else:
            raise UserWorkError('invalid_user_work_snapshot', 503)
    bookmarks = []
    for api_id in rows(raw_preferences['bookmarked_channel_ids'], LIMITS['channels']):
        if not isinstance(api_id, str) or not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', api_id):
            raise UserWorkError()
        # A stale browser bookmark cannot recreate a removed channel in a new backup.
        if api_id in mapping:
            bookmarks.append(mapping[api_id])
    for ref in rows(raw_preferences['pending_bookmark_refs'], LIMITS['channels']):
        # A channel removed for good leaves a stale browser bookmark; skip it
        # like a stale channel ID instead of failing the whole backup.
        if uid(ref) in channels:
            bookmarks.append(ref)
    folders = [{k: row[k] for k in ('id', 'name', 'created_at')} for row in snapshot['folders']]
    known_folders = {row['id'] for row in folders} | {'unassigned'}
    collapsed = [v for v in rows(raw_preferences['collapsed_folder_ids'], 500) if v in known_folders]
    document = normalize_backup({'format': FORMAT, 'version': VERSION, 'created_at': now,
        'user_work': {'folders': folders, 'labels': [{k: r[k] for k in ('label_key', 'name', 'label_type', 'created_at')}
                         for r in snapshot['labels']],
            'channels': list(channels.values()), 'collections': collections,
            'reviews': [owner({**{k: r[k] for k in REVIEW_FIELDS if k != 'history'},
                         'history': [{k: h[k] for k in HISTORY_FIELDS} for h in r['history']]}, r) for r in snapshot['reviews']]},
        'browser_preferences': {'catalogue_density': raw_preferences['catalogue_density'],
            'bookmarked_channel_refs': list(dict.fromkeys(bookmarks)), 'collapsed_folder_ids': collapsed}})
    missing = sum(row['user_input'] is None for row in document['user_work']['channels'])
    return {'backup': document, 'warnings': [{'code': 'channels_need_user_input', 'count': missing}] if missing else []}
