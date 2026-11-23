"""SQL of research_query.operation_kinds.static_details: the database text this operation appends to a population.

Moved from the operation module so that all query construction lives in db/ (CONTRIBUTING.md).
"""
from .inspection import TARGET_JSON
from .inspection import TARGET_SQL
from .common import FINAL_SELECT
from research_query.operation_kinds.inspection import bind_target
from research_query.operation_kinds.static_details import DESCRIPTION


def compile_sql(plan, sql, params):
    bind_target(plan, params)
    description = plan.parameters.get('description') is True
    params['inspect_description'] = description
    # The description columns exist only when the field was a dependency.
    return sql + SQL.replace('{description}', DESCRIPTION if description else ''), params


SQL = """,
""" + TARGET_SQL + FINAL_SELECT.replace('{result}', """jsonb_build_object(""" + TARGET_JSON + """,
 'description',%(inspect_description)s::boolean,
 'row',(SELECT jsonb_build_object('video_id',video_id,'identity_clock',identity_clock,'identity_expiry',identity_expiry,
 'checked_at',checked_at,'metadata_checked_at',metadata_checked_at,
 'title',title,'title_state',title_state,'thumbnail_url',thumbnail_url,'thumbnail_url_state',thumbnail_url_state,
 'duration_seconds',duration_seconds::text,'duration_seconds_state',duration_seconds_state,
 'views',views::text,'views_state',views_state,'likes',likes::text,'likes_state',likes_state,
 'comments',comments::text,'comments_state',comments_state,'published_at',published_at,'published_at_state',published_at_state,
 'category_id',category_id,'category_id_state',category_id_state,'tags',tags,'tags_state',tags_state,
 'title_length',title_length::text,'title_length_state',title_length_state,'tag_count',tag_count::text,'tag_count_state',tag_count_state,
 'like_rate',like_rate,'like_rate_state',like_rate_state,'comment_rate',comment_rate,'comment_rate_state',comment_rate_state{description})
 FROM inspector_target))""")
