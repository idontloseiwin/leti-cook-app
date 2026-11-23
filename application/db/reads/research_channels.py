"""One-row-per-channel source mapping; execution remains in the common owner."""
from hot_storage import RETENTION_HOURS
from .research_sql import _scope_ctes, _channel_fields, _needs_text_key, casefold_parameters, page_parts, frozen, frozen_parameters


def compile_channels(plan,continuation=None,*,population=False):
    request=plan.request
    params={'scope_id':request.scope.id,'window':f'{RETENTION_HOURS} hours','limit':request.page.size+1}
    if frozen(plan):  # compile_query refuses frozen populations and continuations.
        params.update(frozen_parameters())
    if _needs_text_key(request,'channel.title'):params.update(casefold_parameters())
    required,predicate,keyset,ordering,keys=page_parts(plan,continuation,params)
    payload=_channel_fields(plan)
    # The protected profile is normalized once per authorized channel. No video
    # join, inventory requirement, score, global rank or acquisition side effect.
    statement="""WITH args AS MATERIALIZED (SELECT statement_timestamp() AS evaluated_at{args_fresh}),
{scope},
{channel_cte}
raw AS MATERIALIZED (
 SELECT p.channel_id,a.evaluated_at,a.evaluated_at AS identity_clock,
 {identity_expiry} AS identity_expiry{channel_raw}
 FROM scope p CROSS JOIN args a {channel_join}),
normalized AS NOT MATERIALIZED (
 SELECT channel_id,evaluated_at,identity_clock,identity_expiry{channel_normalized} FROM raw),
prepared AS NOT MATERIALIZED (SELECT * FROM normalized),
coverage AS MATERIALIZED (
 SELECT (SELECT count(*) FROM raw WHERE profile_complete) AS channels_complete,
 (SELECT count(*) FROM prepared WHERE NOT ({required})) AS required_missing,
 least((SELECT evaluated_at+interval '120 seconds' FROM args),
 (SELECT min(boundary) FROM raw CROSS JOIN LATERAL (VALUES(identity_expiry){channel_deadlines}) deadlines(boundary)
 WHERE isfinite(boundary) AND boundary>evaluated_at)) AS valid_before),
page AS MATERIALIZED (
 SELECT prepared.*,{sort_keys} AS sort_keys FROM prepared
 WHERE ({required}) AND ({predicate}) AND ({keyset}) ORDER BY {ordering} LIMIT %(limit)s){final}"""
    if frozen(plan):
        # Membership is fenced by the scope revision on every access, so the
        # tracked-channel identity holds for the whole frozen result.
        terms=dict(args_fresh=',statement_timestamp()+%(horizon)s::interval AS fresh_until',
                   identity_expiry='a.fresh_until',sort_keys='NULL::jsonb',final="""
SELECT scope_info.*,coverage.channels_complete,coverage.required_missing,0::bigint AS rejected_rows,0::bigint AS expiring_rows,
 0::bigint AS unresolved_collection_members,0::bigint AS hidden_collection_members,a.evaluated_at,a.fresh_until,
 (SELECT count(*) FROM page) AS frozen_count,
 coalesce((SELECT array_agg(channel_id ORDER BY channel_id COLLATE "C") FROM scope),ARRAY[]::text[]) AS scope_channels,
 CASE WHEN (SELECT count(*) FROM page)<=%(frozen_rows)s THEN
 coalesce((SELECT jsonb_agg(to_jsonb(page) ORDER BY {ordering}) FROM page),'[]'::jsonb) END AS rows
FROM scope_info CROSS JOIN coverage CROSS JOIN args a""")
    else:
        terms=dict(args_fresh='',identity_expiry="a.evaluated_at+interval '120 seconds'",
                   sort_keys='jsonb_build_array({keys})',final="""
SELECT scope_info.*,coverage.*,0::bigint AS rejected_rows,0::bigint AS unresolved_collection_members,
 0::bigint AS hidden_collection_members,a.evaluated_at,
 coalesce((SELECT jsonb_agg(to_jsonb(page) ORDER BY {ordering}) FROM page),'[]'::jsonb) AS rows
FROM scope_info CROSS JOIN coverage CROSS JOIN args a""")
    for key,value in terms.items():
        statement=statement.replace('{'+key+'}',value)
    for key,value in dict(scope=_scope_ctes(request.scope.kind),required=required,predicate=predicate,
        keyset=keyset,ordering=ordering,keys=keys,**payload).items():
        statement=statement.replace('{'+key+'}',value)
    if population:
        statement=statement.partition("\npage AS MATERIALIZED (")[0].rstrip().removesuffix(",")
        statement+=" ,population AS MATERIALIZED (SELECT * FROM prepared WHERE ("+required+") AND ("+predicate+"))"
    return statement,params
