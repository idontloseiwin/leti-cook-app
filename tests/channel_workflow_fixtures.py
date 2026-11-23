"""Scripted native rows for the composed Python-owned channel write protocol.

These are queue entries, not a mocked workflow: production helpers still execute
and tests inspect statement order, final fences, savepoints and rollback.
"""
from psycopg import sql
from channel_initial_error_fixtures import stage_reads as legacy_reads
from db.channel import channel_workflow_updates as writer
from db.channel import channel_initial_error_fields as effect

CHANNEL='UC0000000000000000000001'

def stage_reads(stage,identity=CHANNEL):
    rows=legacy_reads(stage,identity)
    rows[0]['old_estimate_text']=None
    spec=writer._STAGES.get(stage)
    return rows[:1] if spec is not None and spec.no_effect else rows

def stage_answers(stage,identity=CHANNEL,*,changed=True,value=None):
    rows=stage_reads(stage,identity)
    if not changed:return [*rows,None]
    result={'new_text':'{"written":true}'}
    if stage=='record_error':result['value']=value
    return [*rows,result,{'eligible':True,'control_epoch':17},1]

def stage_sql(stage):
    spec=writer._STAGES[stage]
    expressions={name:sql.SQL(value) for name,value in spec.expressions}
    statements=[writer._proposal(expressions,spec.source,spec.predicate)]
    if not spec.no_effect:
        statements.append(effect._FACTS)
        if stage in {'page_finish','page_all','full_settlement','retry_finish'}:statements.append(effect._COMPLETED)
        elif stage=='record_error':statements.append(effect._ERROR)
    fields=tuple(dict.fromkeys((*expressions,*(effect.ERROR_FIELDS if not spec.no_effect else ()))))
    statements.extend([writer._final_statement(fields,returning=spec.returning),writer._DISCOVERY_FACTS,writer._DISCOVERY_INSERT])
    return statements


def workflow_results(stage,identity=CHANNEL,*,changed=True,value=None):
    from test_direct_pg_transactions import Result
    return [Result((row,)) if isinstance(row,dict) else
            Result() if row is None else Result(has_columns=False,rowcount=row)
            for row in stage_answers(stage,identity,changed=changed,value=value)]

def metadata_results(identity=CHANNEL,*,capture=False):
    from test_direct_pg_transactions import Result
    rows=[{**legacy_reads('pending',identity)[0],'old_estimate_text':None},
          {'new_text':'{"metadata":"written"}'},{'capture':capture}]
    result=[Result((row,)) for row in rows]
    if capture:result.append(Result(has_columns=False,rowcount=1))
    return result
