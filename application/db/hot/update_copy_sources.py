"""One finite, read-only observation of API acquisition clocks for update copies.

The shared registry owns the startup sources. Optional absent observations are
not missing evidence. No provider values or IDs leave this query.
"""
from types import MappingProxyType
from .source_clocks import SOURCE_CLOCKS, SourceClock

_UNTOUCHED = """source.status='running' AND source.coverage='partial'
 AND source.pages_completed=0 AND source.items_seen=0 AND source.new_memberships_found=0
 AND source.resume_page_token IS NULL AND source.last_transition_id IS NULL AND source.completed_at IS NULL"""

# A locally replaced attempt can end before its first page. Its administrative
# state is not evidence of an API observation; every other scan still needs one.
_EMPTY_REPLACED = """source.status='abandoned' AND source.coverage='partial'
 AND source.error_code='full_initial_generation_replaced' AND source.source_checked_at IS NULL
 AND source.pages_completed=0 AND source.items_seen=0 AND source.new_memberships_found=0
 AND source.resume_page_token IS NULL AND source.last_transition_id IS NULL
 AND source.last_transition_kind IS NULL AND source.completed_at IS NULL
 AND NOT EXISTS(SELECT 1 FROM public.channel_playlist_state state
     WHERE state.active_scan_id=source.scan_id OR state.last_completed_scan_id=source.scan_id)"""
_NO_OBSERVATION = '(' + _UNTOUCHED + ') OR (' + _EMPTY_REPLACED + ')'


def _row(expression, from_sql, predicate='true', *, inclusive=False):
    return (f'SELECT ({expression}) AS source_at,{str(inclusive).lower()} AS inclusive '
            f'FROM {from_sql} WHERE ({predicate})')


def _simple(name, spec):
    payload = spec.payload
    if name == 'memberships_first':
        payload = 'first_seen_at IS NOT NULL OR first_seen_scan_id IS NOT NULL'
    elif name == 'memberships_present':
        payload = "last_present_at IS NOT NULL OR source_published_at IS NOT NULL OR membership_status='present'"
    elif name == 'playlist_source':
        payload = """source_checked_at IS NOT NULL OR expected_item_count IS NOT NULL
 OR inventory_completed_at IS NOT NULL OR last_completed_scan_id IS NOT NULL
 OR inventory_status IN('completed','playlist_not_found')"""
    elif name == 'inventory_receipts':
        payload = "result IS DISTINCT FROM '{\"source_expired\":true}'::jsonb OR claims IS NOT NULL OR next_page_token_hash IS NOT NULL"
    elif name == 'inventory_scans_source_checked_at':
        payload = '(' + payload + ') AND NOT coalesce(source.source_checked_at IS NULL AND (' + _NO_OBSERVATION + '),false)'
    return _row(spec.expression, 'public.' + spec.table + ' source', payload, inclusive=spec.inclusive)


_sources = {name: _simple(name, spec) for name, spec in SOURCE_CLOCKS.items() if isinstance(spec, SourceClock)}
_sources['channel_inventory_estimate'] = _row('parsed.at', """public.channels source
 CROSS JOIN LATERAL unnest(CASE WHEN initial_inventory_estimate->>'scope'='all'
    THEN ARRAY['long','short','stream'] ELSE ARRAY['long'] END) kinds(kind)
 CROSS JOIN LATERAL (SELECT CASE WHEN initial_inventory_estimate ? 'checked_at_by_type'
    THEN initial_inventory_estimate->'checked_at_by_type'->>kind
    ELSE initial_inventory_estimate->>'checked_at' END AS value) raw
 CROSS JOIN LATERAL (SELECT CASE WHEN pg_input_is_valid(raw.value,'timestamptz')
    THEN raw.value::timestamptz END AS at) parsed""", """initial_inventory_estimate IS NOT NULL AND (
 raw.value IS NOT NULL OR initial_inventory_estimate->'counts'->kind IS DISTINCT FROM 'null'::jsonb
 OR initial_inventory_estimate->'statuses'->>kind IN('ok','playlist_not_found'))""")

# Left joins retain missing referenced sources as NULL evidence instead of
# silently making a fresh job look independent of its old scan/receipt.
_sources['operator_jobs_linked_scans'] = _row('observed.at', """public.operator_jobs job
 CROSS JOIN LATERAL (
   SELECT job.inventory_scan_id::text AS value WHERE job.inventory_scan_id IS NOT NULL
   UNION ALL SELECT job.summary_json->'inventory_ack'->>'scan_id'
      WHERE job.summary_json->'inventory_ack'->>'scan_id' IS NOT NULL
   UNION ALL SELECT job.summary_json->'full_inventory_sources'->kind->>'scan_id'
      FROM unnest(ARRAY['long','short','stream']) kinds(kind)
      WHERE job.initial_scope_version=1 AND job.summary_json->'full_inventory_sources' ? kind
 ) refs
 LEFT JOIN public.channel_video_inventory_scans source ON source.scan_id=CASE
   WHEN pg_input_is_valid(refs.value,'uuid') THEN refs.value::uuid END
 CROSS JOIN LATERAL (VALUES (source.started_at,true),(source.scan_boundary_at,true),
   (source.source_checked_at,NOT coalesce(""" + _NO_OBSERVATION + """,false)),
   (source.completed_at,source.status='completed')) observed(at,required)""",
    'observed.at IS NOT NULL OR observed.required')
_sources['operator_jobs_ack_receipts'] = _row('receipt.source_checked_at', """public.operator_jobs job
 LEFT JOIN public.typed_inventory_receipts receipt ON receipt.transition_id=
    CASE WHEN pg_input_is_valid(job.summary_json->'inventory_ack'->>'transition_id','uuid')
    THEN (job.summary_json->'inventory_ack'->>'transition_id')::uuid END
    AND receipt.receipt_scope=CASE WHEN job.summary_json->'inventory_ack'->>'outcome'='quota'
    THEN 'quota_defer' ELSE 'scan_transition' END""",
    "job.summary_json->'inventory_ack'->>'outcome' IN('page','playlist_not_found','error','quota')")
_sources['operator_chain_linked_scans'] = _row('observed.at', """public.operator_job_inventory_chains child
 CROSS JOIN LATERAL (
   SELECT child.scan_id::text AS value WHERE child.scan_id IS NOT NULL
   UNION ALL SELECT child.last_ack->>'scan_id' WHERE child.last_ack->>'scan_id' IS NOT NULL
 ) refs
 LEFT JOIN public.channel_video_inventory_scans source ON source.scan_id=CASE
   WHEN pg_input_is_valid(refs.value,'uuid') THEN refs.value::uuid END
  AND source.channel_id=child.channel_id AND source.video_type=child.video_type
 CROSS JOIN LATERAL (VALUES (source.started_at,true),(source.scan_boundary_at,true),
   (source.source_checked_at,NOT coalesce(""" + _NO_OBSERVATION + """,false)),
   (source.completed_at,source.status='completed')) observed(at,required)""",
    'observed.at IS NOT NULL OR observed.required')
_sources['operator_chain_ack_receipts'] = _row('receipt.source_checked_at', """public.operator_job_inventory_chains child
 LEFT JOIN public.typed_inventory_receipts receipt ON receipt.transition_id=
    CASE WHEN pg_input_is_valid(child.last_ack->>'transition_id','uuid')
    THEN (child.last_ack->>'transition_id')::uuid END
    AND receipt.receipt_scope=CASE WHEN child.last_ack->>'outcome'='quota'
    THEN 'quota_defer' ELSE 'scan_transition' END""",
    "child.last_ack->>'outcome' IN('page','playlist_not_found','error','quota')")

assert set(_sources) == set(SOURCE_CLOCKS)
UPDATE_COPY_SOURCE_ROWS = MappingProxyType({name: _sources[name] for name in SOURCE_CLOCKS})
del _sources

# Canonical type remains derived from live membership or a local decision.
# Publication and queue copies use their own clocks in the shared registry.
_UNPROVED = """EXISTS(SELECT 1 FROM public.video_inventory i WHERE i.video_type IS NOT NULL
 AND NOT EXISTS(SELECT 1 FROM public.video_type_review_cases decision WHERE decision.video_id=i.video_id
   AND decision.channel_id=i.channel_id AND decision.decision_type=i.video_type)
 AND NOT EXISTS(SELECT 1 FROM public.video_inventory_memberships m WHERE m.video_id=i.video_id
   AND m.channel_id=i.channel_id AND m.video_type=i.video_type AND m.membership_status='present'
   AND isfinite(m.last_present_at) AND isfinite(m.last_seen_at)))
 OR EXISTS(SELECT 1 FROM public.channel_playlist_state p WHERE
   (p.active_scan_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM public.channel_video_inventory_scans s WHERE s.scan_id=p.active_scan_id))
   OR (p.last_completed_scan_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM public.channel_video_inventory_scans s WHERE s.scan_id=p.last_completed_scan_id)))"""

# These clocks have an ordered index with a matching expression and a predicate
# covering the update-copy source. Composite cleanup keys for other sources do
# not order their individual clocks; keep those as one aggregate per source.
_INDEXED_SOURCES = frozenset({
    'video_counters', 'video_metadata', 'video_availability',
    'channel_profile', 'channel_country', 'video_snapshots', 'channel_snapshots',
    'typed_video_collection_receipts', 'availability_events',
    'availability_incidents_restored', 'sync_runs', 'quota_attempts',
    'category_reference',
    'inventory_publication', 'queue_publication', 'queue_stream_end', 'review_evidence',
})


def _source_facts(name, rows):
    spec = SOURCE_CLOCKS[name]
    expires = isinstance(spec, SourceClock) and spec.expires
    upper = "(clock.at + interval '696 hours')" if expires else 'clock.at'
    # A deadline is ordered natively through its own index. Convert only the
    # resulting minimum to an equivalent acquisition time for the copy policy.
    offset = " - interval '696 hours'" if expires else ''
    if name not in _INDEXED_SOURCES:
        return """SELECT (min(source_at) FILTER(WHERE isfinite(source_at)))""" + offset + """ AS oldest_source_at,
 coalesce(bool_or(inclusive),false) AS inclusive,
 coalesce(bool_or(source_at IS NULL OR NOT isfinite(source_at) OR source_at>""" + upper + """),false) AS invalid_source_clock
 FROM (""" + rows + ') observed CROSS JOIN clock'

    # Preserve the exact source projection and payload predicate above. PostgreSQL
    # can pull up this simple subquery and seek its underlying clock index.
    source = 'FROM (' + rows + ') observed WHERE '
    oldest = ("SELECT source_at " + source
              + "source_at>'-infinity'::timestamptz AND source_at<'infinity'::timestamptz "
              + 'ORDER BY source_at LIMIT 1')
    # A finite future clock still contributes to the minimum. Read the maximum
    # before comparing it with this run's clock: a correlated range predicate
    # can choose a full scan to prove that no future timestamps exist.
    invalid = ' OR '.join('EXISTS(SELECT 1 ' + source + predicate + ')' for predicate in (
        'source_at IS NULL', "source_at='-infinity'::timestamptz",
    ))
    latest = ('SELECT source_at ' + source
              + 'source_at IS NOT NULL ORDER BY source_at DESC LIMIT 1')
    invalid += f' OR coalesce(({latest})>{upper},false)'
    inclusive = str(SOURCE_CLOCKS[name].inclusive).lower()
    return (f'SELECT ({oldest}){offset} AS oldest_source_at,{inclusive} AS inclusive,'
            f'({invalid}) AS invalid_source_clock FROM clock')


# The profile cleanup key maps every non-ok state to -infinity. Only known
# workflow states with a finite, nonfuture physical observation can explain
# that sentinel. This does not reinterpret that observation as acquisition.
_RETAINED_PROFILE = """(source.channel_metadata_status IS NULL
 OR source.channel_metadata_status IN('channel_not_found','error','running'))
 AND source.channel_metadata_checked_at IS NOT NULL
 AND isfinite(source.channel_metadata_checked_at)
 AND source.channel_metadata_checked_at<=clock.at"""
_PROFILE = SOURCE_CLOCKS['channel_profile']
_PROFILE_OTHER_INVALID = f"""EXISTS(SELECT 1 FROM public.channels source CROSS JOIN clock
 WHERE ({_PROFILE.payload}) AND (({_PROFILE.expression}) IS NULL
   OR NOT isfinite({_PROFILE.expression}) OR ({_PROFILE.expression})>clock.at)
 AND NOT coalesce(({_RETAINED_PROFILE}),false))"""

UPDATE_COPY_WINDOW_SQL = """WITH clock AS MATERIALIZED(SELECT clock_timestamp() AS at),
 sources AS (""" + '\n UNION ALL '.join(
    "SELECT '" + name + "'::text AS source_name,observed.* FROM (" + _source_facts(name, rows) + ') observed'
    for name, rows in UPDATE_COPY_SOURCE_ROWS.items()
) + """), facts AS (
 SELECT min(oldest_source_at) AS oldest_source_at,
 coalesce(bool_or(invalid_source_clock),false) AS invalid_source_clock,
 coalesce(bool_or(invalid_source_clock) FILTER(WHERE source_name<>'channel_profile'),false) AS other_invalid_clock,
 coalesce(bool_or(isfinite(oldest_source_at) AND (oldest_source_at<clock.at-interval '696 hours'
   OR (inclusive AND oldest_source_at=clock.at-interval '696 hours'))),false) AS expired
 FROM sources CROSS JOIN clock), proof AS MATERIALIZED (
 SELECT (""" + _UNPROVED + ') AS unproved,(' + _PROFILE_OTHER_INVALID + """) AS profile_other_invalid)
 SELECT to_jsonb(clock.at)#>>'{}' AS as_of,to_jsonb(facts.oldest_source_at)#>>'{}' AS oldest_source_at,
 facts.expired,(facts.invalid_source_clock OR proof.unproved) AS invalid_source_clock,
 2 AS version,(facts.invalid_source_clock AND NOT facts.other_invalid_clock
   AND NOT proof.unproved AND NOT proof.profile_other_invalid) AS retained_profile_only
 FROM facts CROSS JOIN clock CROSS JOIN proof"""
