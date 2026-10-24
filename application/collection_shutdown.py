"""Cooperative worker handoff, checked only between completed work units.

The supervisor writes one command to its own child's stdin. This also works
while that child is still importing modules: no startup signal can kill it.
No HTTP process installs the reader. Database leases remain the write owner.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import threading

_requested = threading.Event()
_deferred = ContextVar("collection_handoff_deferred", default=False)


def request_handoff():
    """Stop at the next boundary; never resume this process's old work pass."""
    _requested.set()


def read_control(stream):
    # EOF means the supervising pipe was closed. Finish the current unit too.
    if stream.readline(16) in (b"stop\n", b""):
        request_handoff()


def start_control_reader(stream):
    # Read the raw pipe. A thread still waiting on a buffered stream holds its
    # lock, and Python then aborts the worker's own exit (SIGABRT, code -6).
    stream = getattr(stream, "raw", stream)
    threading.Thread(target=read_control, args=(stream,), daemon=True,
                     name="collection-handoff").start()


def handoff_requested():
    """Stop new parallel dispatch even while a parent job ACK is deferred."""
    return _requested.is_set()


def yield_requested():
    return _requested.is_set() and not _deferred.get()


def stop_at_boundary(summary):
    if not yield_requested():
        return False
    summary.update(yield_requested=True, stopped_reason="scheduled_handoff")
    return True


@contextmanager
def finish_current_chunk():
    """An operator chunk includes its durable job acknowledgement."""
    token = _deferred.set(True)
    try:
        yield
    finally:
        _deferred.reset(token)


def release_unstarted_playlists(repository, claims, summary):
    """Release only known, undispatched leases, once; never retry unknown ACKs."""
    if not claims:
        return
    from db.hot.hot_collection_control import abort_pending_playlists
    try:
        abort_pending_playlists(repository.database, claims)
    except Exception:
        summary["errors"] = int(summary.get("errors") or 0) + 1
        summary["settlement_unknown"] = True
        # The existing expiry policy recovers any still-owned leases.


# Private supervisor/child protocol, not a user configuration setting. Only
# source-free physical positions cross this boundary; durable work stays in DB.
CURSOR_SNAPSHOT_ENV = 'LIC_COLLECTION_CURSOR_SNAPSHOT'
CURSOR_COMPLETE_PREFIX = b'collection_cursor_complete='
CURSOR_MAX_BYTES = 4096


def validate_cursor_checkpoint(value):
    """Exact versioned bounded snapshot; no payload/claim/receipt fields allowed."""
    from datetime import datetime, timedelta, timezone
    from user_channel_input import parse_user_channel_input
    def shape(item, names):
        if type(item) is not dict or set(item) != set(names):
            raise ValueError('collection_cursor_checkpoint_invalid')
    def flag(item):
        if type(item) is not bool:
            raise ValueError('collection_cursor_checkpoint_invalid')
    def integer(item, minimum, maximum):
        if type(item) is not int or not minimum <= item <= maximum:
            raise ValueError('collection_cursor_checkpoint_invalid')
    def identity(item):
        try:
            if type(item) is not str:raise ValueError()
            _,kind,lookup=parse_user_channel_input(item)
            if kind!='id' or lookup!=item:raise ValueError()
        except ValueError:raise ValueError('collection_cursor_checkpoint_invalid') from None
    def utc(item):
        if type(item) is not str or len(item) != 32:
            raise ValueError('collection_cursor_checkpoint_invalid')
        try:parsed=datetime.fromisoformat(item)
        except ValueError:raise ValueError('collection_cursor_checkpoint_invalid') from None
        if parsed.utcoffset() != timedelta(0) or parsed.astimezone(timezone.utc).isoformat(timespec='microseconds') != item:
            raise ValueError('collection_cursor_checkpoint_invalid')
    shape(value, ('version','common','metadata','inventory'))
    # This checkpoint lives only in the running supervisor and its children.
    # A backend restart drops it. Version 2's shared frontier cannot represent
    # independent operation progress and must never be silently reinterpreted.
    if type(value['version']) is not int or value['version'] != 3:
        raise ValueError('collection_cursor_checkpoint_invalid')
    common=value['common'];kinds=('long','short','stream','unknown')
    operations=('metadata','counters')
    shape(common,('after','formats','exhausted','revisit'))
    shape(common['after'],operations);shape(common['exhausted'],operations)
    flag(common['revisit'])
    if (type(common['formats']) is not list or len(common['formats']) != 4
            or any(type(kind) is not str for kind in common['formats'])
            or set(common['formats']) != set(kinds)):
        raise ValueError('collection_cursor_checkpoint_invalid')
    for operation in operations:
        shape(common['after'][operation],kinds)
        exhausted=common['exhausted'][operation]
        if (type(exhausted) is not list or len(exhausted) > 4
                or any(type(kind) is not str or kind not in kinds for kind in exhausted)
                or len(set(exhausted)) != len(exhausted)):
            raise ValueError('collection_cursor_checkpoint_invalid')
        for item in common['after'][operation].values():
            if item is not None:identity(item)
    metadata=value['metadata'];shape(metadata,('after','due_ceiling','exhausted'));flag(metadata['exhausted'])
    if metadata['due_ceiling'] is not None:utc(metadata['due_ceiling'])
    if metadata['after'] is not None:
        shape(metadata['after'],('next_after','channel_id'));identity(metadata['after']['channel_id'])
        if metadata['due_ceiling'] is None:raise ValueError('collection_cursor_checkpoint_invalid')
        if metadata['after']['next_after'] is not None:
            utc(metadata['after']['next_after'])
            if metadata['after']['next_after'] > metadata['due_ceiling']:
                raise ValueError('collection_cursor_checkpoint_invalid')
    inventory=value['inventory'];shape(inventory,('after','ceiling','exhausted'));flag(inventory['exhausted'])
    if inventory['ceiling'] is not None:integer(inventory['ceiling'],0,2**63-1)
    if inventory['after'] is not None:
        shape(inventory['after'],('dispatch_ordinal','priority','channel_id','video_type'))
        after=inventory['after'];integer(after['dispatch_ordinal'],1,2**63-1)
        integer(after['priority'],-2**31,2**31-1);identity(after['channel_id'])
        if (type(after['video_type']) is not str or after['video_type'] not in kinds[:3]
                or inventory['ceiling'] is None or after['dispatch_ordinal'] > inventory['ceiling']):
            raise ValueError('collection_cursor_checkpoint_invalid')
    return value


def encode_cursor_checkpoint(value):
    import json
    try:encoded=json.dumps(validate_cursor_checkpoint(value),sort_keys=True,separators=(',',':'),ensure_ascii=True)
    except (ValueError,TypeError,KeyError):raise ValueError('collection_cursor_checkpoint_invalid') from None
    if len(encoded.encode('ascii')) > CURSOR_MAX_BYTES:
        raise ValueError('collection_cursor_checkpoint_invalid')
    return encoded


def decode_cursor_checkpoint(encoded):
    import json
    if type(encoded) is not str or not encoded.isascii() or len(encoded) > CURSOR_MAX_BYTES:
        raise ValueError('collection_cursor_checkpoint_invalid')
    def unique(pairs):
        result={}
        for key,item in pairs:
            if key in result:raise ValueError('collection_cursor_checkpoint_invalid')
            result[key]=item
        return result
    try:value=json.loads(encoded,object_pairs_hook=unique)
    except (ValueError,TypeError):raise ValueError('collection_cursor_checkpoint_invalid') from None
    try:return validate_cursor_checkpoint(value)
    except (ValueError,TypeError,KeyError):raise ValueError('collection_cursor_checkpoint_invalid') from None
