"""Whole-operation stale-run recovery on one writer transaction.

One fixed cursor retains one statement snapshot and locks one qualifying row at
fetch time. There is no total row cutoff or fresh-page query. The caller owns
one transaction and its total deadline; any failure rolls back all updates.
"""
from collections.abc import Mapping
from psycopg import sql
from ..base.postgres import DatabaseError
from .sync_run_write_normalization import _native_row, _final_summary, _update_statement

_RECOVERY_ROW = _native_row({
    'status': sql.SQL("'error'::text"),
    'finished_at': sql.SQL('coalesce(run.finished_at,%(finished_at)s::timestamptz,now())'),
    'errors': sql.SQL('greatest(coalesce(run.errors,0),1)'),
    'summary_json': sql.SQL("""coalesce(run.summary_json,'{}'::jsonb)
      || CASE WHEN run.summary_json->>'summary_schema_version'='3'
         AND ((run.summary_json->>'column_fields')::integer & 4)<>0
         THEN jsonb_build_object('finished_at',run.finished_at) ELSE '{}'::jsonb END
      || jsonb_build_object('errors',coalesce(run.summary_json->'errors',to_jsonb(run.errors)),
                           'stale_run_recovered',true,'reason','process_exit_without_finish')"""),
})
_RECOVERY_CANDIDATES = sql.SQL("""SELECT run.id,to_jsonb({row})::text AS columns_text,
 ({row}).summary_json::text AS summary_text FROM public.sync_runs run
 WHERE run.status='running' AND run.started_at<%(before)s::timestamptz
 FOR NO KEY UPDATE OF run""").format(row=_RECOVERY_ROW)
_RECOVERY_UPDATE = _update_statement(('status','finished_at','errors','summary_json'),'run.id=%s::bigint')


def recover_stale_sync_runs(transaction, *, before, finished_at):
    """Borrow one owner transaction after the public cutoff trim/empty check."""
    with transaction.borrow():
        count=0
        with transaction.sync_run_recovery_candidates(before=before,finished_at=finished_at) as candidates:
            for row in candidates:
                if (not isinstance(row,Mapping) or set(row)!={'id','columns_text','summary_text'}
                        or type(row['id']) is not int):
                    raise DatabaseError('database_result_shape_invalid')
                proposed={key:row[key] for key in ('columns_text','summary_text')}
                summary=_final_summary(transaction,proposed)
                changed=transaction.execute(_RECOVERY_UPDATE,(summary,row['columns_text'],row['id']))
                if changed!=1:
                    raise DatabaseError('database_result_shape_invalid')
                count+=1
        return count
