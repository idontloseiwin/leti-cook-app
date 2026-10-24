"""Closed source-free cancellation of exact, acknowledged metadata markers."""
from datetime import datetime

_ORDER = "SELECT value AS row FROM jsonb_array_elements(%s::jsonb) ORDER BY value->>'channel_id'"
_LOCK = """SELECT channel_metadata_status AS status, channel_metadata_started_at AS started_at
 FROM public.channels WHERE channel_id=%s FOR UPDATE NOWAIT"""
_CONTROL = "SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s FOR SHARE NOWAIT"
_CLEAR = """UPDATE public.channels SET channel_metadata_status=NULL,channel_metadata_started_at=NULL
 WHERE channel_id=%s AND channel_metadata_status='running' AND channel_metadata_started_at=%s::timestamptz
 RETURNING channel_id"""


def validate(rows):
    if not isinstance(rows, (list, tuple)) or len(rows) > 50:
        raise ValueError('hot_metadata_markers_invalid')
    clean, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'channel_id', 'control_epoch', 'started_at'}:
            raise ValueError('hot_metadata_markers_invalid')
        identity, epoch, started = row['channel_id'], row['control_epoch'], row['started_at']
        if (type(identity) is not str or not 1 <= len(identity) <= 128
                or any(ord(char) < 33 or ord(char) == 127 for char in identity)
                or identity in seen or type(epoch) is not int or not 1 <= epoch <= 9223372036854775807
                or type(started) is not str):
            raise ValueError('hot_metadata_markers_invalid')
        try:
            parsed = datetime.fromisoformat(started.replace('Z', '+00:00'))
        except ValueError:
            raise ValueError('hot_metadata_markers_invalid') from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('hot_metadata_markers_invalid')
        seen.add(identity)
        clean.append(dict(row))
    return clean


def drain_hot_channel_metadata(transaction, markers):
    from psycopg.types.json import Jsonb
    markers = validate(markers)
    with transaction.borrow():
        rows = transaction.fetch_all(_ORDER, (Jsonb(markers),), max_rows=len(markers)) if markers else []
        if len(rows) != len(markers):
            raise RuntimeError('hot_metadata_drain_unconfirmed')
        for entry in rows:
            marker = entry['row']
            identity, epoch, started = marker['channel_id'], marker['control_epoch'], marker['started_at']
            held = transaction.fetch_one(_LOCK, (identity,))
            control = transaction.fetch_one(_CONTROL, (identity,))
            if (held is None or control is None or control['control_epoch'] != epoch
                    or held['status'] != 'running'
                    or held['started_at'] != datetime.fromisoformat(started.replace('Z', '+00:00'))):
                raise RuntimeError('hot_metadata_marker_mismatch')
            row = transaction.returning_one(_CLEAR, (identity, started))
            if row is None or row.get('channel_id') != identity:
                raise RuntimeError('hot_metadata_drain_unconfirmed')
            after = transaction.fetch_one(_LOCK, (identity,))
            if after is None or after['status'] is not None or after['started_at'] is not None:
                raise RuntimeError('hot_metadata_drain_unconfirmed')
    return {'status': 'drained', 'requested': len(markers), 'released': len(markers), 'remaining': 0}
