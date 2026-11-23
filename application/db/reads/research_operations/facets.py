"""SQL of research_query.operation_kinds.facets: the database text this operation appends to a population.

Moved from the operation module so that all query construction lives in db/ (CONTRIBUTING.md).
"""
from .common import data_final_select
from research_query.operation_kinds.facets import EXCLUDED
from research_query.operation_kinds.facets import GROUPS
from research_query.operation_kinds.facets import _parameters
from research_query.registry import PRESETS_VERSION as BUCKET_VERSION
from research_query.registry import preset_buckets as buckets


def compile_sql(plan, sql, params):
    # SQL-only imports load at compile time; validation stays import-light.
    from psycopg.types.json import Jsonb
    from db.reads.research_sql import MAPPINGS, _condition as condition_sql, local_publication
    groups, conditions = _parameters(plan.parameters)
    video_type = plan.query.request.video_types[0]
    frozen = plan.binding is not None
    flags = {name: 'coalesce((' + condition_sql(conditions[name], params) + '),false)' if name in conditions else 'true'
             for name in groups}

    def others(name):
        return ' AND '.join('f_' + other for other in groups if other != name) or 'true'

    def keep(name):
        # Frozen: a listed row counts in every group; another row only where its own condition fails.
        return f'(member OR NOT f_{name}) AND {others(name)}' if frozen else others(name)

    columns = [MAPPINGS[GROUPS[name].field][0] + ' AS v_' + name for name in groups]
    columns += [flags[name] + ' AS f_' + name for name in groups]
    if not frozen:
        ctes = ['facet_rows AS MATERIALIZED (\n SELECT ' + ','.join(columns) + ' FROM population)']
    else:
        params.update(plan.binding.parameters)
        arrays, names, values = ['%(facet_frozen_ids)s::text[]'], ['video_id'], []
        for name in groups:
            if name == 'published' and params['facet_frozen_instant']:
                # The canonical local year of the frozen instant, in the request's zone.
                local, instant, in_range = local_publication('f.published_instant', '%(query_time_zone)s::text')
                arrays.append('%(facet_frozen_published)s::timestamptz[]')
                names.append('published_instant')
                values.append(f'CASE WHEN {instant} AND {in_range} THEN extract(year FROM {local}) END AS v_{name}')
            else:
                kind = 'text' if GROUPS[name].kind in ('categories', 'values') else 'numeric'
                arrays.append(f'%(facet_frozen_{name})s::{kind}[]')
                names.append('v_' + name)
                values.append(f'f.v_{name}')
        members = ('SELECT f.video_id,true AS member,' + ','.join(values) + ',' + ','.join(f'true AS f_{name}' for name in groups)
                   + ' FROM unnest(' + ','.join(arrays) + ') AS f(' + ','.join(names) + ')')
        current = ('SELECT video_id,false AS member,' + ','.join(columns)
                   + ' FROM population WHERE NOT (video_id=ANY(%(facet_frozen_ids)s::text[]))')
        ctes = ['facet_rows AS MATERIALIZED (\n ' + members + '\n UNION ALL\n ' + current + ')']
    result = []
    numeric = [name for name in groups if GROUPS[name].kind == 'buckets']
    if numeric:
        params['facet_buckets'] = Jsonb([dict(grp=name, key=bucket.key, label=bucket.label, lower=bucket.min,
                                              upper=bucket.max, ordinal=index)
                                         for name in numeric
                                         for index, bucket in enumerate(buckets(name, video_type))])
        rows = ',\n '.join(f"('{name}',r.v_{name},{keep(name)})" for name in numeric)
        ctes += ['facet_values AS MATERIALIZED (\n SELECT v.grp,v.value FROM facet_rows r CROSS JOIN LATERAL (VALUES\n '
                 + rows + ') v(grp,value,keep) WHERE v.keep)',
                 'facet_totals AS (SELECT grp,count(*) AS count,count(value) AS known FROM facet_values GROUP BY grp)',
                 'facet_bucket_counts AS (\n SELECT b.grp,b.key,b.label,b.lower,b.upper,b.ordinal,count(v.value) AS count\n'
                 ' FROM jsonb_to_recordset(%(facet_buckets)s::jsonb) b(grp text,key text,label text,lower numeric,upper numeric,ordinal integer)\n'
                 ' LEFT JOIN facet_values v ON v.grp=b.grp AND v.value IS NOT NULL\n'
                 ' AND (b.lower IS NULL OR v.value>=b.lower) AND (b.upper IS NULL OR v.value<b.upper)\n'
                 ' GROUP BY b.grp,b.key,b.label,b.lower,b.upper,b.ordinal)']
        for name in numeric:
            result.append(f"'{name}',jsonb_build_object('field','{GROUPS[name].field}',"
                          f"'count',coalesce((SELECT count FROM facet_totals WHERE grp='{name}'),0),"
                          f"'unknown_count',coalesce((SELECT count-known FROM facet_totals WHERE grp='{name}'),0),"
                          f"'buckets',(SELECT jsonb_agg(jsonb_build_object('key',key,'label',label,'min',lower,'max',upper,"
                          f"'count',count) ORDER BY ordinal) FROM facet_bucket_counts WHERE grp='{name}'))")
    if 'published' in groups:
        # Year choices come from every admitted row, never the active query (Library rule);
        # with a frozen list, its own years too.
        options = ('SELECT DISTINCT published_year AS year FROM prepared WHERE published_year IS NOT NULL' if not frozen else
                   'SELECT year FROM (SELECT published_year AS year FROM prepared WHERE published_year IS NOT NULL'
                   ' UNION SELECT v_published FROM facet_rows WHERE member AND v_published IS NOT NULL) y')
        ctes += [f'facet_year_options AS ({options} ORDER BY 1 DESC LIMIT 4)',
                 'facet_latest_years AS (SELECT year FROM facet_year_options ORDER BY year DESC LIMIT 3)',
                 f"facet_published AS MATERIALIZED (SELECT v_published AS year FROM facet_rows WHERE {keep('published')})"]
        result.append("'published',jsonb_build_object('field','video.published_year',"
                      "'count',(SELECT count(*) FROM facet_published),"
                      "'unknown_count',(SELECT count(*) FROM facet_published WHERE year IS NULL),"
                      "'years',coalesce((SELECT jsonb_agg(jsonb_build_object('year',y.year::integer,'count',"
                      "(SELECT count(*) FROM facet_published p WHERE p.year=y.year)) ORDER BY y.year DESC) FROM facet_latest_years y),'[]'::jsonb),"
                      "'older',CASE WHEN (SELECT count(*) FROM facet_year_options)>3 THEN jsonb_build_object("
                      "'before_year',(SELECT min(year) FROM facet_latest_years)::integer,"
                      "'count',(SELECT count(*) FROM facet_published p WHERE p.year<(SELECT min(year) FROM facet_latest_years))) END)")
    if 'category' in groups:
        # Every category of the admitted rows stays a choice, with its count.
        options = ("SELECT DISTINCT category_id AS id FROM prepared WHERE category_id IS NOT NULL AND category_id<>''"
                   + ("" if not frozen else " UNION SELECT v_category FROM facet_rows WHERE member AND v_category<>''"))
        ctes += [f"facet_category_options AS ({options})",
                 f"facet_category AS MATERIALIZED (SELECT v_category AS id FROM facet_rows WHERE {keep('category')})",
                 "facet_category_counts AS (SELECT id,count(*) AS count FROM facet_category"
                 " WHERE id IS NOT NULL AND id<>'' GROUP BY id)"]
        result.append("'category',jsonb_build_object('field','video.category_id',"
                      "'count',(SELECT count(*) FROM facet_category),"
                      "'unknown_count',(SELECT count(*) FROM facet_category WHERE id IS NULL OR id=''),"
                      "'categories',coalesce((SELECT jsonb_agg(jsonb_build_object('id',o.id,'count',coalesce(c.count,0))"
                      " ORDER BY o.id COLLATE \"C\") FROM facet_category_options o LEFT JOIN facet_category_counts c USING(id)),'[]'::jsonb))")
    for name in (n for n in groups if GROUPS[n].kind == 'values'):
        # A closed vocabulary: every value is listed, in its order, with its count.
        params['facet_values_' + name] = list(GROUPS[name].values)
        ctes.append(f"facet_{name} AS MATERIALIZED (SELECT v_{name} AS value FROM facet_rows WHERE {keep(name)})")
        result.append(f"'{name}',jsonb_build_object('field','{GROUPS[name].field}',"
                      f"'count',(SELECT count(*) FROM facet_{name}),"
                      f"'unknown_count',(SELECT count(*) FROM facet_{name} WHERE value IS NULL),"
                      f"'values',(SELECT jsonb_agg(jsonb_build_object('value',v.value,'count',"
                      f"(SELECT count(*) FROM facet_{name} g WHERE g.value=v.value)) ORDER BY v.ordinal)"
                      f" FROM unnest(%(facet_values_{name})s::text[]) WITH ORDINALITY v(value,ordinal)))")
    every = ' AND '.join('f_' + name for name in groups)
    params.update(facet_time_zone=plan.query.request.time.timezone, facet_bucket_version=BUCKET_VERSION)
    excluded = ','.join(f"'{reason}',count(*) FILTER(WHERE rejection='{reason}')" for reason in EXCLUDED)
    if not frozen:
        population = ("jsonb_build_object('kind','query','count',(SELECT count(*) FROM population),"
                      f"'matched_count',(SELECT count(*) FROM facet_rows WHERE {every}),"
                      "'expired_counters',(SELECT count(*) FROM population WHERE views_state='expired'),"
                      "'expired_metadata',(SELECT count(*) FROM population WHERE title_state='expired'),"
                      f"'excluded',(SELECT jsonb_build_object({excluded}) FROM raw WHERE NOT admitted))")
    else:
        # The frozen rows (with their frozen states) and the current rows outside them;
        # a frozen row rejected now is still one of the list's rows, not an exclusion.
        outside = 'NOT (video_id=ANY(%(facet_frozen_ids)s::text[]))'
        population = ("jsonb_build_object('kind','frozen','count',(SELECT count(*) FROM facet_rows),"
                      "'matched_count',(SELECT count(*) FROM facet_rows WHERE member),"
                      "'expired_counters',%(facet_frozen_expired_counters)s::integer"
                      f"+(SELECT count(*) FROM population WHERE views_state='expired' AND {outside}),"
                      "'expired_metadata',%(facet_frozen_expired_metadata)s::integer"
                      f"+(SELECT count(*) FROM population WHERE title_state='expired' AND {outside}),"
                      f"'excluded',(SELECT jsonb_build_object({excluded}) FROM raw WHERE NOT admitted AND {outside}))")
    # Counts live by the data clocks of their inputs (coverage.data_valid_before),
    # not the 120 s of a current page; presets and years never change by time alone.
    final = data_final_select("jsonb_build_object('population'," + population + ","
                              "'time_zone',%(facet_time_zone)s::text,'bucket_registry_version',%(facet_bucket_version)s::integer,"
                              "'groups',jsonb_build_object(" + ',\n '.join(result) + '))')
    return sql + ',\n' + ',\n'.join(ctes) + final, params
