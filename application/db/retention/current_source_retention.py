"""Bounded cleanup of current API fields, preserving source clocks/user work.

Used by the runtime retention repository. PostgreSQL owns range selection, native clocks and
locked predicates; Python owns the finite protocol and per-type estimate policy.
"""
from dataclasses import dataclass
import json
from types import MappingProxyType
from psycopg.types.json import Jsonb
from .channel_estimate_retention import reset_inventory_estimate
from .queue_retention import busy_videos
from ..base.postgres import DatabaseServerError

ERRORS=frozenset({'hot_current_cleanup_invalid_limit','hot_current_cleanup_invalid_target','hot_current_cleanup_invalid_cursor'})

@dataclass(frozen=True)
class Source:
    table:str
    identity:str
    clock:str|None
    key:str
    fields:tuple[str,...]
    @property
    def payload(self):return ' OR '.join(field+' IS NOT NULL' for field in self.fields)

_TARGETS=MappingProxyType({
 'video_counters':Source('videos','video_id','checked_at',"coalesce(checked_at,'-infinity'::timestamptz)",('views','like_count','comment_count')),
 'video_metadata':Source('videos','video_id','metadata_checked_at',"coalesce(metadata_checked_at,'-infinity'::timestamptz)",('title','published_at','duration','tags','category_id','youtube_topic_categories','default_audio_language','default_language','caption_available','stream_actual_end_at','publication_status','scheduled_start_at')),
 'video_availability':Source('videos','video_id','availability_checked_at',"coalesce(availability_checked_at,'-infinity'::timestamptz)",('availability_status','privacy_status','upload_status')),
 'channel_profile':Source('channels','channel_id','channel_metadata_checked_at',"CASE WHEN channel_metadata_status='ok' THEN coalesce(channel_metadata_checked_at,'-infinity'::timestamptz) ELSE '-infinity'::timestamptz END",('title','avatar_url','subscribers','views','video_count','channel_created_at','uploads_playlist_id')),
 'channel_country':Source('channels','channel_id','country_collected_at',"coalesce(country_collected_at,'-infinity'::timestamptz)",('country_code',)),
 'channel_inventory_estimate':Source('channels','channel_id',None,"'-infinity'::timestamptz",('initial_inventory_estimate',)),
})
_HEADER="""SELECT %s::text AS target,%s::text AS after_id,%s::integer AS limit,
 to_jsonb(statement_timestamp())#>>'{}' AS at,
 to_jsonb(statement_timestamp()-interval '696 hours')#>>'{}' AS cutoff"""


def _reject(message):raise DatabaseServerError(message,code='P0001')


def _cursor(tx,value,target):
    if value is None:return '-infinity',''
    try:
        with tx.savepoint():
            row=tx.fetch_one("""WITH input AS (SELECT %s::text AS raw), decoded AS(SELECT raw,raw::jsonb AS value FROM input)
                SELECT octet_length(raw)<=1024 AND jsonb_typeof(value)='object'
                 AND jsonb_typeof(value->'id')='string' AND jsonb_typeof(value->'clock')='string'
                 AND value->>'target' IS NOT DISTINCT FROM %s::text AND length(value->>'id') BETWEEN 1 AND 128 AS valid,
                 value->>'id' AS id,to_jsonb((value->>'clock')::timestamptz)#>>'{}' AS clock FROM decoded""",(value,target))
            if row['valid'] is not True:_reject('hot_current_cleanup_invalid_cursor')
            return row['clock'],row['id']
    except DatabaseServerError as error:
        if error.code in ('57014','P0004'):raise
        _reject('hot_current_cleanup_invalid_cursor')


def _page(spec,estimate):
    # Only fixed module-owned identifiers/fragments enter SQL construction.
    if estimate:
        inner="SELECT channel_id AS id,'-infinity'::timestamptz AS source_key FROM public.channels WHERE channel_id>%(after_id)s ORDER BY channel_id LIMIT %(size)s"
    else:
        base=f'SELECT {spec.identity} AS id,{spec.key} AS source_key FROM public.{spec.table} WHERE ({spec.payload}) AND '
        cursor=f' AND (({spec.key}),{spec.identity})>(%(after_clock)s::timestamptz,%(after_id)s::text)'
        order=f' ORDER BY ({spec.key}),{spec.identity} LIMIT %(size)s'
        inner='('+base+f'({spec.key})<%(cutoff)s::timestamptz'+cursor+order+') UNION ALL ('+base+f'({spec.key})>%(at)s::timestamptz'+cursor+order+')'
    return """SELECT id,to_jsonb(source_key)#>>'{}' AS source_key,
        NOT isfinite(source_key) OR source_key>%(at)s::timestamptz AS invalid_clock,
        jsonb_build_object('target',%(target)s::text,'clock',source_key,'id',id)::text AS cursor
        FROM ("""+inner+") page ORDER BY page.source_key,page.id LIMIT %(size)s"


def _row(tx,spec,identity,locked=False):
    # Native text survives Python transport without float underflow/overflow.
    # In particular, malformed historical estimate counts must reach the native
    # predicates unchanged even during profile/country cleanup of the same row.
    estimate=',t.initial_inventory_estimate::text AS estimate_json' if spec.table=='channels' else ''
    return tx.fetch_one(f'SELECT to_jsonb(t)::text AS value{estimate} FROM public.{spec.table} t WHERE {spec.identity}=%s'+(' FOR UPDATE SKIP LOCKED' if locked else ''),(identity,))


def _predicate(spec,target):
    clock=f't.{spec.clock}'
    predicate=f'({clock} IS NULL OR NOT isfinite({clock}) OR {clock}<%(cutoff)s::timestamptz OR {clock}>clock_timestamp()'
    # Marking a refresh as running changes neither the stored profile nor its
    # clock, so the clock alone decides then (as the startup expiry check does);
    # any other status but ok withdraws the profile at once.
    if target=='channel_profile':predicate+=" OR t.channel_metadata_status IS NULL OR t.channel_metadata_status NOT IN ('ok','running')"
    return predicate+')'


def _expired(tx,spec,target,row,cutoff,*,payload):
    predicate=_predicate(spec,target)
    if payload:predicate='('+spec.payload+') AND '+predicate
    return tx.fetch_one(f'SELECT {predicate} AS expired FROM jsonb_populate_record(NULL::public.{spec.table},%(row)s::jsonb) t',dict(row=row['value'],cutoff=cutoff))['expired'] is True


def _estimate(tx,original,cutoff):
    if original is None:return None,False
    # Only shape and the scope label are inspected here. All numeric decisions
    # use the original JSONB text below, never a decoded Python number.
    decoded=json.loads(original,parse_int=str,parse_float=str)
    if decoded is None:return None,False
    estimate=decoded if isinstance(decoded,dict) else {}
    changed=not isinstance(decoded,dict) or bool(set(estimate)-{'scope','video_types','counts','statuses','checked_at_by_type','checked_at'})
    kinds=['long','short','stream'] if estimate.get('scope')=='all' else ['long']
    counts={}
    statuses={}
    clocks={}
    keep=0
    # Three typed observations at most. Native timestamp errors are isolated;
    # numeric JSON syntax/range stays PostgreSQL's, not Python int coercion.
    for kind in kinds:
        parsed=None
        try:
            with tx.savepoint():
                parsed=tx.fetch_one("""WITH input AS(SELECT %s::jsonb AS estimate,%s::text AS kind), clock AS(
                    SELECT CASE WHEN estimate ? 'checked_at_by_type' THEN (estimate->'checked_at_by_type'->>kind)::timestamptz
                      ELSE (estimate->>'checked_at')::timestamptz END AS at FROM input)
                    SELECT to_jsonb(at)#>>'{}' AS at FROM clock""",(original,kind))['at']
        except DatabaseServerError as error:
            if error.code not in ('22007','22008'):raise
        row=tx.fetch_one("""WITH input AS(SELECT %s::jsonb AS estimate,%s::text AS kind,%s::timestamptz AS at), checked AS(
            SELECT input.*,coalesce(at IS NOT NULL AND isfinite(at) AND at>=%s::timestamptz AND at<=clock_timestamp()
             AND ((jsonb_typeof(estimate->'counts'->kind)='number'
               AND CASE WHEN (estimate->'counts'->kind)::text ~ '^[0-9]{1,10}$' THEN (estimate->'counts'->>kind)::bigint<=2147483647 ELSE false END
               AND estimate->'statuses'->>kind='ok')
              OR (estimate->'statuses'->>kind='playlist_not_found' AND estimate->'counts'->kind IN('null'::jsonb,'0'::jsonb))),false) AS keep FROM input)
            SELECT keep,CASE WHEN keep THEN estimate->'counts'->kind END AS count,
             CASE WHEN keep THEN estimate->'statuses'->kind END AS status,
             estimate->'counts'->kind IS DISTINCT FROM 'null'::jsonb OR at IS NOT NULL AS changed FROM checked""",
             (original,kind,parsed,cutoff))
        if row['keep']:
            counts[kind]=row['count']
            statuses[kind]=row['status']
            clocks[kind]=parsed
            keep+=1
        else:
            counts[kind]=None
            statuses[kind]='error'
            clocks[kind]=None
            changed=changed or row['changed']
    result=dict(scope='all' if len(kinds)==3 else 'long',video_types=kinds,counts=counts,statuses=statuses,checked_at_by_type=clocks)
    return result if keep else None,bool(changed or not keep)


def _video_page(tx,spec,target,rows,cutoff):
    """Video sources: the row protocol below, one statement per step for the page.

    Expired rows with a payload whose collector does not own the video are
    locked and scrubbed; the scrub rechecks the locked rows, so a refresh that
    committed in between wins. Returns (scrubbed, deferred rows in page order).
    """
    if not rows:return 0,[]
    predicate=_predicate(spec,target)
    proof=tx.fetch_all(f"""SELECT c.ord FROM unnest(%(ids)s::text[]) WITH ORDINALITY c(id,ord)
        JOIN public.videos t ON t.video_id=c.id WHERE ({spec.payload}) AND {predicate} ORDER BY c.ord""",
        dict(ids=[row['id'] for row in rows],cutoff=cutoff),max_rows=len(rows))
    candidates=[rows[check['ord']-1] for check in proof]
    busy=busy_videos(tx,[row['id'] for row in candidates])
    free=[row['id'] for row in candidates if row['id'] not in busy]
    locked=tx.fetch_all("""SELECT c.id FROM unnest(%(ids)s::text[]) c(id)
        CROSS JOIN LATERAL(SELECT 1 FROM public.videos t WHERE t.video_id=c.id FOR UPDATE SKIP LOCKED) l""",
        dict(ids=free),max_rows=len(free)) if free else []
    owned={row['id'] for row in locked}
    deferred=[row for row in candidates if row['id'] not in owned]
    if not owned:return 0,deferred
    fields=','.join(name+'=NULL' for name in spec.fields)
    return tx.execute(f'UPDATE public.videos t SET {fields} WHERE t.video_id=ANY(%(ids)s::text[]) AND ({spec.payload}) AND {predicate}',
                      dict(ids=sorted(owned),cutoff=cutoff)),deferred


def cleanup_hot_current_source(transaction,*,target,after_id=None,limit=100,references=None):
    tx=transaction
    with tx.borrow():
        args=tx.fetch_one(_HEADER,(target,after_id,limit))
        target=args['target']
        limit=args['limit']
        if limit is None or not 1<=limit<=500:_reject('hot_current_cleanup_invalid_limit')
        if target not in _TARGETS:_reject('hot_current_cleanup_invalid_target')
        spec=_TARGETS[target]
        estimate=target=='channel_inventory_estimate'
        after_clock,cursor_id=_cursor(tx,args['after_id'],target)
        params=dict(target=target,after_clock=after_clock,after_id=cursor_id,at=args['at'],cutoff=args['cutoff'],size=limit+1)
        if references is None:
            selected=tx.fetch_all(_page(spec,estimate),params,max_rows=limit+1)
            complete=len(selected)<=limit
            rows=selected[:limit]
        else:
            # Private exact-key entry, validated by cleanup_hot_priority. Missing
            # rows are resolved; busy existing rows remain visible to this read.
            params['ids']=[key[0] for key in references]
            rows=tx.fetch_all(f"""SELECT {spec.identity} AS id,to_jsonb({spec.key})#>>'{{}}' AS source_key,
                NOT isfinite({spec.key}) OR ({spec.key})>%(at)s::timestamptz AS invalid_clock,
                {spec.identity} AS cursor FROM public.{spec.table}
                WHERE {spec.identity}=ANY(%(ids)s::text[]) ORDER BY {spec.identity}""",params,max_rows=len(references))
            complete=True
        scrubbed=0
        deferred=[]
        videos=spec.table=='videos'
        if videos:scrubbed,deferred=_video_page(tx,spec,target,rows,args['cutoff'])
        for candidate in () if videos else rows:
            row=_row(tx,spec,candidate['id'])
            if row is None:continue
            if estimate:
                proposed,changed=_estimate(tx,row['estimate_json'],args['cutoff'])
                if not changed:continue
            elif not _expired(tx,spec,target,row,args['cutoff'],payload=True):continue
            row=_row(tx,spec,candidate['id'],locked=True)
            if row is None:deferred.append(candidate);continue
            if estimate:
                proposed,changed=_estimate(tx,row['estimate_json'],args['cutoff'])
                if changed:
                    reset_inventory_estimate(tx,candidate['id'],proposed)
                    scrubbed+=1
            elif _expired(tx,spec,target,row,args['cutoff'],payload=False):
                fields=','.join(name+'=NULL' for name in spec.fields)
                scrubbed+=tx.execute(f'UPDATE public.{spec.table} SET {fields} WHERE {spec.identity}=%s AND ({spec.payload})',(candidate['id'],))
        invalid=sum(bool(row['invalid_clock']) for row in deferred) if not estimate else 0
        expired=[row['source_key'] for row in deferred if not row['invalid_clock']] if not estimate else []
        oldest=None
        if expired:oldest=tx.fetch_one("SELECT to_jsonb(min(value::timestamptz))#>>'{}' AS value FROM unnest(%s::text[]) clocks(value)",(expired,))['value']
        # Unlike evidence cleanup, this protocol retains the last visited cursor
        # even on an exhausted nonempty page. Its lookahead defines completion.
        return dict(status='ok',target=target,as_of=args['at'],cutoff=args['cutoff'],scanned=len(rows),deleted=0,
            scrubbed=scrubbed,deferred=len(deferred),next_cursor=rows[-1]['cursor'] if rows else None,complete=complete,
            telemetry_scope='scanned_page',oldest_remaining_expired_source_at=oldest,invalid_clock_remaining=invalid,
            priority_complete=not estimate and len(deferred)<=100,
            priority_references=[dict(target=target,key=[row['id']]) for row in deferred[:100]] if not estimate else [])
