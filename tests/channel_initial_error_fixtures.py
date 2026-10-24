"""Native row/effect result fixtures shared by active writer boundary tests."""
from db.channel import channel_initial_error_updates as writer
from db.channel import channel_initial_error_fields as effect

CHANNEL = 'UC0000000000000000000001'

def stage_reads(stage, identity=CHANNEL):
    completed = stage in {'page_finish','page_all','full_settlement','retry_finish','completed'}
    errored = stage == 'record_error'
    rows = [dict(identity=identity,old_text='{"old":true}',proposed_text='{"new":true}',estimate_text=None),
            dict(proposed_text='{"new":true}',completed=completed,errored=errored,was_error=False,previous_count=2)]
    if completed or errored:
        rows.append(dict(proposed_text='{"normalized":true}'))
    return rows

def stage_sql(stage):
    spec=writer._STAGES[stage]
    if spec.no_effect:return [spec.original()]
    statements=[spec.proposal(),effect._FACTS]
    if stage in {'page_finish','page_all','full_settlement','retry_finish'}:statements.append(effect._COMPLETED)
    elif stage=='record_error':statements.append(effect._ERROR)
    statements.append(writer._final_statement(tuple(name for name,_ in spec.expressions),returning=spec.returning))
    return statements
