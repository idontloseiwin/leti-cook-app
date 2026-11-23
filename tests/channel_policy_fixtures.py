"""Native Boolean/NULL rows for accepted channel policy reader tests.

These are explicit no-job/legacy-job fixtures, not a policy implementation.
Callers choose a fixture state; the real shared policy classifies it.
"""
def native_policy_facts(mode='active'):
 facts=dict(control_present=True,hidden=False,pause_hidden=False,
  permission_accepted=True,permission_not_accepted=False,permission_manual=False,
  linked=False,job_missing=True,job_channel_distinct=True,job_type_distinct=True,
  epoch_null=True,errors_null=True,status_null=True,phase_null=True,
  types_long=False,types_all=False,type_count_three=None,scope_full=None,
  status_blocked=None,status_error=None,status_suspended=None,status_completed=None,
  frozen_null=True,errors_zero=None,phase_verify=None,phase_initial_stats=None,
  initial_completed=True,initial_skipped=False)
 if mode=='active':pass
 # Historical fixture name: terminal empty Long now permits every video type.
 elif mode=='metadata_only':facts.update(initial_completed=False,initial_skipped=True)
 elif mode=='operator_hold':facts.update(permission_accepted=False,permission_not_accepted=True)
 elif mode=='hidden':facts.update(hidden=True,pause_hidden=True)
 elif mode=='manual_required':facts.update(permission_accepted=False,permission_not_accepted=True,permission_manual=True)
 elif mode=='initial_inventory':facts.update(linked=True,job_missing=False,job_channel_distinct=False,job_type_distinct=False,
  epoch_null=False,errors_null=False,status_null=False,phase_null=False,types_long=True,type_count_three=False,scope_full=False,
  status_blocked=False,status_error=False,status_suspended=False,status_completed=False,errors_zero=True,phase_verify=False,
  phase_initial_stats=False,initial_completed=False)
 else:raise AssertionError('unknown native channel fixture: '+repr(mode))
 return facts

def native_eligible_facts(eligible):
 assert type(eligible) is bool
 return native_policy_facts('active' if eligible else 'operator_hold')

def native_pause_facts(reason):
 return native_policy_facts({None:'active','channel_hidden':'hidden','channel_no_long_playlist':'metadata_only',
  'channel_inventory_not_ready':'operator_hold','channel_inventory_manual_required':'manual_required'}[reason])
