"""Canonical type-repair preview.

One aggregate statement supplies the complete preview snapshot. PostgreSQL owns
exact evidence joins/counts/MD5 ordering; Python owns the repair admission
decision and public result. The repair writer separately owns its fixed
table-lock protocol. No provider requests or mutation.
"""
from contextlib import contextmanager
from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseServerError,Operation
from ..readiness.intrinsic_capabilities import READINESS_SQL as _FINAL_READINESS_SQL

_INPUT = 'SELECT %s::integer AS example_limit'

_CANDIDATES = """select
            resolution.video_id,
            resolution.canonical_channel_id,
            resolution.canonical_video_type,
            resolution.present_membership_types[1] as authoritative_video_type
        from public.video_inventory_type_resolution as resolution
        where resolution.resolution_status = 'canonical_unsupported'
          and resolution.present_channel_count = 1
          and resolution.present_type_count = 1
          and resolution.present_membership_channels[1] =
                resolution.canonical_channel_id
          and resolution.present_membership_types[1] in ('short', 'stream')
          and exists (
              select 1
              from public.video_inventory_memberships as membership
              where membership.video_id = resolution.video_id
                and membership.channel_id = resolution.canonical_channel_id
                and membership.video_type =
                      resolution.present_membership_types[1]
                and membership.membership_status = 'present'
                and membership.evidence_source = 'typed_playlist_scan'
          )
          and (
              resolution.canonical_video_type is null
              or resolution.canonical_video_type = 'long'
          )
          and resolution.canonical_video_type is distinct from
                resolution.present_membership_types[1]"""
_READ = "WITH totals(canonical_unsupported,explicit_conflicts,typed_to_typed) AS (\nselect\n        count(*) filter (\n            where resolution.resolution_status = 'canonical_unsupported'\n        ),\n        count(*) filter (\n            where resolution.resolution_status = 'conflict'\n        ),\n        count(*) filter (\n            where resolution.resolution_status = 'canonical_unsupported'\n              and resolution.canonical_video_type in ('short', 'stream')\n        )\n    from public.video_inventory_type_resolution as resolution), candidates AS (\n" + _CANDIDATES + "), metrics(repairable_count,long_to_short,long_to_stream,null_to_short,null_to_stream,collection_state_rows,candidate_active_lease_rows,current_video_rows,snapshot_video_rows,candidate_fingerprint) AS (\nselect\n        count(*),\n        count(*) filter (\n            where candidate.canonical_video_type = 'long'\n              and candidate.authoritative_video_type = 'short'\n        ),\n        count(*) filter (\n            where candidate.canonical_video_type = 'long'\n              and candidate.authoritative_video_type = 'stream'\n        ),\n        count(*) filter (\n            where candidate.canonical_video_type is null\n              and candidate.authoritative_video_type = 'short'\n        ),\n        count(*) filter (\n            where candidate.canonical_video_type is null\n              and candidate.authoritative_video_type = 'stream'\n        ),\n        count(*) filter (\n            where exists (\n                select 1\n                from public.video_collection_state as collection\n                where collection.video_id = candidate.video_id\n            )\n        ),\n        count(*) filter (\n            where exists (\n                select 1\n                from public.video_collection_state as collection\n                where collection.video_id = candidate.video_id\n                  and collection.lease_token is not null\n                  and collection.lease_expires_at > now()\n            )\n        ),\n        count(*) filter (\n            where exists (\n                select 1\n                from public.videos as video\n                where video.video_id = candidate.video_id\n            )\n        ),\n        count(*) filter (\n            where exists (\n                select 1\n                from public.video_snapshots as snapshot\n                where snapshot.video_id = candidate.video_id\n            )\n        ),\n        pg_catalog.md5(\n            coalesce(\n                string_agg(\n                    pg_catalog.concat_ws(\n                        '|',\n                        candidate.video_id,\n                        candidate.canonical_channel_id,\n                        coalesce(candidate.canonical_video_type, '<null>'),\n                        candidate.authoritative_video_type\n                    ),\n                    E'\n'\n                    order by candidate.video_id\n                ),\n                ''\n            )\n        )\n    from candidates as candidate), sample AS (SELECT * FROM candidates ORDER BY video_id LIMIT %(example_limit)s::integer)\nSELECT totals.*,metrics.*,\n (SELECT count(*) FROM public.channel_playlist_state WHERE video_type IN('short','stream') AND runtime_enabled) AS runtime_enabled_rows,\n (SELECT count(*) FROM public.channel_video_inventory_state WHERE video_type IN('short','stream') AND product_exposure_ready) AS product_exposure_rows,\n (SELECT count(*) FROM public.channel_video_inventory_scans WHERE video_type IN('short','stream') AND status='running') AS running_scan_rows,\n (SELECT roles_valid AND functions_valid AND triggers_valid FROM (" + _FINAL_READINESS_SQL + ") final_integrity) AS collection_trigger_ready,\n (SELECT coalesce(jsonb_agg(jsonb_build_object('video_id',video_id,'channel_id',canonical_channel_id,\n  'canonical_video_type',canonical_video_type,'authoritative_video_type',authoritative_video_type) ORDER BY video_id),'[]'::jsonb) FROM sample) AS examples\nFROM totals CROSS JOIN metrics"


@contextmanager
def _transaction(database, transaction):
    if transaction is not None:
        with transaction.borrow():
            yield transaction
    else:
        spec = get_operation('inspect_typed_inventory_canonical_repair')
        with database.transaction(Operation(spec.name, read_only=True,
                statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,
                total_timeout_ms=spec.statement_timeout_ms,
                error_codes=spec.error_codes)) as owned:
            yield owned


def _result(row):
    unsafe = row['canonical_unsupported'] - row['repairable_count']
    blocked = (row['explicit_conflicts'] > 0 or unsafe > 0
        or row['runtime_enabled_rows'] > 0 or row['product_exposure_rows'] > 0
        or row['running_scan_rows'] > 0 or row['candidate_active_lease_rows'] > 0
        or not row['collection_trigger_ready'] or row['repairable_count'] > 5000)
    return dict(status='blocked' if blocked else 'nothing_to_repair' if row['repairable_count'] == 0 else 'ready',
        schema_version=1, authoritative_evidence='unique_present_typed_playlist_membership',
        canonical_channel_mutation=False, max_atomic_repairs=5000,
        repairable_count=row['repairable_count'], candidate_fingerprint=row['candidate_fingerprint'],
        long_to_short=row['long_to_short'], long_to_stream=row['long_to_stream'],
        null_to_short=row['null_to_short'], null_to_stream=row['null_to_stream'],
        canonical_unsupported_total=row['canonical_unsupported'], explicit_conflicts=row['explicit_conflicts'],
        unsafe_unsupported=unsafe, typed_to_typed_mismatches=row['typed_to_typed'],
        runtime_enabled_rows=row['runtime_enabled_rows'],product_exposure_ready_rows=row['product_exposure_rows'],
        running_typed_scan_rows=row['running_scan_rows'], collection_state_rows_to_remove=row['collection_state_rows'],
        candidate_active_collection_lease_rows=row['candidate_active_lease_rows'],
        current_video_rows_retained=row['current_video_rows'],video_ids_with_snapshots_retained=row['snapshot_video_rows'],
        # Historical API field: readiness of the current mechanism, including Python-owned effects.
        collection_state_trigger_ready=row['collection_trigger_ready'],examples=row['examples'])


def inspect_typed_inventory_canonical_repair(database, example_limit=0, *, transaction=None):
    with _transaction(database, transaction) as tx:
        value = tx.fetch_one(_INPUT, (example_limit,))['example_limit']
        if value is None or not 0 <= value <= 25:
            raise DatabaseServerError('typed_inventory_canonical_repair_example_limit_invalid', code='P0001')
        return _result(tx.fetch_one(_READ, dict(example_limit=value)))
