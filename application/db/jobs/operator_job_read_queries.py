"""Bounded job status readers.

PostgreSQL selects and orders rows; latest reads recheck current control in a
second statement for the captured job, matching the retained VOLATILE wrapper.
Python projects suspension without changing persisted fields. Active lists use
one statement. No stored business helper, locks, write or per-row query; max100
rows.
"""
from contextlib import contextmanager
from ..base.operation_contracts import get_operation
from ..base.repository import DatabaseRepository
from ..base.postgres import DatabaseError,Operation
from ..base.sql_values import application_value

_ELIGIBLE=("""EXISTS(SELECT 1 FROM readable.channel_control control
 WHERE control.channel_id=job.channel_id AND NOT control.is_hidden
 AND control.owner_permission='accepted' AND control.control_epoch=job.accepted_epoch
 AND ((job.job_type='channel_initial_long_inventory' AND control.initial_job_id=job.id)
 OR (job.job_type='channel_current_stats_backfill' AND control.collection_mode='active')))""")
_ACTIVE="job.status IN('queued','running') AND "+_ELIGIBLE
_LATEST="""SELECT to_jsonb(job) AS value FROM readable.operator_jobs job WHERE job.channel_id=%s::text AND job.job_type=%s::text
 ORDER BY CASE WHEN """+_ACTIVE+""" THEN 0 ELSE 1 END,job.requested_at DESC,job.id DESC LIMIT 1"""
_LIST="SELECT job.* FROM readable.operator_jobs job WHERE "+_ACTIVE+"""
 ORDER BY job.requested_at,job.id LIMIT greatest(least(coalesce(%s::integer,20),100),0)"""
_EFFECTIVE="""WITH job AS (SELECT %s::text AS channel_id,%s::bigint AS accepted_epoch,
 %s::uuid AS id,%s::text AS job_type,%s::text AS status)
 SELECT job.status IN('queued','running') AND NOT """+_ELIGIBLE+" AS suspended FROM job"
_CAPTURED_FIELDS=('channel_id','accepted_epoch','id','job_type','status')
_SUSPENDED=dict(status='suspended',lease_token=None,lease_owner=None,lease_expires_at=None,
 lease_channel_epoch=None,last_error_code='channel_sync_not_ready',last_error_summary='Channel control generation changed.')

@contextmanager
def _read(database,name):
 spec=get_operation(name)
 with database.transaction(Operation(name,read_only=True,statement_timeout_ms=spec.statement_timeout_ms,
         lock_timeout_ms=spec.lock_timeout_ms,error_codes=spec.error_codes)) as tx:yield tx

def _latest(database,name,job_type,channel_id):
 with _read(database,name) as tx:
  row=tx.fetch_one(_LATEST,(channel_id,job_type))
  if row is None:return None
  if not isinstance(row,dict) or set(row)!={'value'} or not isinstance(row['value'],dict) or not set(_CAPTURED_FIELDS)<=set(row['value']):
   raise DatabaseError('database_result_shape_invalid')
  value=dict(row['value'])
  DatabaseRepository(database).admit_source_deadlines([value])
  effective=tx.fetch_one(_EFFECTIVE,tuple(value[key] for key in _CAPTURED_FIELDS))
  if not isinstance(effective,dict) or set(effective)!={'suspended'} or type(effective['suspended']) is not bool:
   raise DatabaseError('database_result_shape_invalid')
  if effective['suspended']:value.update(_SUSPENDED)
  return application_value(value)

def get_operator_job_by_id(database, job_id, *, channel_id, job_type):
 """Read the committed receipt's identity, never a concurrently newer latest job."""
 with _read(database, 'get_operator_job_status') as tx:
  row=tx.fetch_one(
   "SELECT to_jsonb(job) AS value FROM readable.operator_jobs job "
   "WHERE job.id=%s::uuid AND job.channel_id=%s::text AND job.job_type=%s::text",
   (job_id,channel_id,job_type))
  if row is None:return None
  if not isinstance(row,dict) or set(row)!={'value'} or not isinstance(row['value'],dict) or not set(_CAPTURED_FIELDS)<=set(row['value']):
   raise DatabaseError('database_result_shape_invalid')
  value=dict(row['value'])
  DatabaseRepository(database).admit_source_deadlines([value])
  effective=tx.fetch_one(_EFFECTIVE,tuple(value[key] for key in _CAPTURED_FIELDS))
  if not isinstance(effective,dict) or set(effective)!={'suspended'} or type(effective['suspended']) is not bool:
   raise DatabaseError('database_result_shape_invalid')
  if effective['suspended']:value.update(_SUSPENDED)
  return application_value(value)

def get_operator_job_status(database,channel_id):
 return _latest(database,'get_operator_job_status','channel_current_stats_backfill',channel_id)

def get_channel_initial_inventory_job(database,channel_id):
 return _latest(database,'get_channel_initial_inventory_job','channel_initial_long_inventory',channel_id)

def list_active_operator_jobs(database,limit=20):
 with _read(database,'list_active_operator_jobs') as tx:
  rows=DatabaseRepository(database).admit_source_deadlines(tx.fetch_all(_LIST,(limit,),max_rows=100))
  return application_value(rows)
