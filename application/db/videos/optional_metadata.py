"""Optional metadata writes borrowed by the existing leased profile owner."""
from psycopg.types.json import Jsonb
from research_field_policy import normalize, effective, FieldPolicyError
from research_field_policy import normalize_observation

_LOCK = "SELECT revision,document FROM public.research_preferences WHERE workspace_id='installation' FOR SHARE"
_UPSERT = """INSERT INTO public.video_optional_metadata(video_id,
 description,description_folded,description_checked_at,description_policy_revision,
 paid_promotion,paid_promotion_checked_at,paid_promotion_policy_revision)
 SELECT input.video_id,input.description,input.description_folded,
 CASE WHEN input.description_enabled THEN input.observed_at END,
 CASE WHEN input.description_enabled THEN input.revision END,input.paid_promotion,
 CASE WHEN input.paid_enabled THEN input.observed_at END,CASE WHEN input.paid_enabled THEN input.revision END
 FROM jsonb_to_recordset(%s::jsonb) input(video_id text,lease_token uuid,channel_id text,
 observed_at timestamptz,revision bigint,description_enabled boolean,paid_enabled boolean,
 description text,description_folded text,paid_promotion boolean)
 JOIN public.video_collection_state state ON state.video_id=input.video_id
 AND state.lease_token=input.lease_token AND state.channel_id=input.channel_id
 ON CONFLICT(video_id) DO UPDATE SET
 description=CASE WHEN excluded.description_policy_revision IS NOT NULL THEN excluded.description ELSE video_optional_metadata.description END,
 description_folded=CASE WHEN excluded.description_policy_revision IS NOT NULL THEN excluded.description_folded ELSE video_optional_metadata.description_folded END,
 description_checked_at=coalesce(excluded.description_checked_at,video_optional_metadata.description_checked_at),
 description_policy_revision=coalesce(excluded.description_policy_revision,video_optional_metadata.description_policy_revision),
 paid_promotion=CASE WHEN excluded.paid_promotion_policy_revision IS NOT NULL THEN excluded.paid_promotion ELSE video_optional_metadata.paid_promotion END,
 paid_promotion_checked_at=coalesce(excluded.paid_promotion_checked_at,video_optional_metadata.paid_promotion_checked_at),
 paid_promotion_policy_revision=coalesce(excluded.paid_promotion_policy_revision,video_optional_metadata.paid_promotion_policy_revision)"""

def complete(tx, rows):
    candidates=[row for row in rows if row.get('optional_observation') is not None]
    if not candidates:return
    current=tx.fetch_one(_LOCK,())
    if current is None:raise FieldPolicyError('field_preferences_unavailable',503)
    document=normalize(current['document'])
    # Already locked lease rows are the sole type/write owner, never API input.
    kinds={row['video_id']:row['video_type'] for row in tx.fetch_all(
        'SELECT video_id,video_type FROM public.video_collection_state WHERE video_id=ANY(%s::text[])',
        ([row['video_id'] for row in candidates],),max_rows=len(candidates))}
    output=[]
    for row in candidates:
        observation=normalize_observation(row['optional_observation'])
        proof=observation['proof'];kind=kinds.get(row['video_id'])
        if proof['revision']!=current['revision'] or kind not in proof['effective']:continue
        modes=effective(document,kind)
        if modes!=proof['effective'][kind]:continue
        desc=modes['video.description']=='current';paid=modes['video.paid_promotion']=='current'
        if not (desc or paid):continue
        description=observation['values'].get('video.description') if desc else None
        output.append(dict(video_id=row['video_id'],channel_id=row['channel_id'],lease_token=row['lease_token'],
            observed_at=row.get('metadata_observed_at') or row['recorded_at'],revision=current['revision'],
            description_enabled=desc,paid_enabled=paid,description=description,
            description_folded=description.casefold() if description is not None else None,
            paid_promotion=observation['values'].get('video.paid_promotion') if paid else None))
    if output:tx.execute(_UPSERT,(Jsonb(output),))
