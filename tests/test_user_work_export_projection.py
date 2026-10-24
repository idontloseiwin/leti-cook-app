"""Export-only projection experiment: local relational parity and exact SQL delta.

SQLite below checks join/projection algebra, not PostgreSQL JSONB bytes, RLS or
snapshot behavior. Native retained/direct and performance acceptance is pending.
"""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.user_work import user_work_read_queries as q
from db.base.postgres import _reviewed_statement
from test_user_work_read_queries import COUNTS,Database

BEFORE_HASHES={
 '_EXPORT_CAPACITY':'915582765a51e1a7af73c45806982009157a5baff241a9ecd2a5ed9dee44682f',
 '_EXPORT_ESTIMATE':'e1321a96a6d709c65517da38446677edcd9d360351225b44f8e1f90c48a8cd01',
 '_EXPORT_DOCUMENT':'f2b0bc4b30065c366f4e75cecdaf8fb9d9c05da6596beaf22af717016d483aac'}
NARROW_FROM='FROM public.channels c LEFT JOIN public.channel_collection_state control ON control.channel_id=c.channel_id'
FIELDS=('channel_id','user_work_id','user_input','folder_id','parent_channel_id','is_hidden','hidden_at','added_at')


def before_reader_views(statement):
    """Undo only R07f's three source-free reader relation substitutions."""
    return (statement.replace('readable.channel_user_work', 'public.channels')
            .replace('readable.channel_control', 'public.channel_collection_state')
            .replace('readable.video_type_user_work', 'public.video_type_review_cases'))


def pre59_document(statement):
    """Undo only the accepted59 wrappers, then require the exact reviewed pre-59 SQL (by SHA-256)."""
    statement=before_reader_views(statement)
    reverse=[];sections=[]
    for line in statement.splitlines(keepends=True):
        if '(WITH projected AS MATERIALIZED (SELECT ' not in line:
            reverse.append(line);continue
        prefix,tail=line.split('(WITH projected AS MATERIALIZED (SELECT ',1)
        projected,rest=tail.rsplit(') SELECT ',1)
        aggregate,suffix=rest.rsplit(' FROM projected)',1)
        reverse.append(prefix+'(SELECT '+aggregate+' FROM (SELECT '+projected+') projected)'+suffix)
        sections.append(prefix.strip().split("'")[1])
    if sections!=['folders','labels','channels','assignments','saved_objects','members','pending','reviews']:
        raise AssertionError('accepted59 section wrappers changed')
    result=''.join(reverse)
    if hashlib.sha256(result.encode()).hexdigest()!='3a653a2274a19af8ae45a224444ef2bb7227d8c3e8f4069668c59f6092966eea':
        raise AssertionError('accepted59 changed more than section materialization')
    return result


def previous(statement):
    return before_reader_views(statement).replace('SELECT 1 FROM public.channels LIMIT 10001','SELECT 1 FROM public.channels_with_control LIMIT 10001').replace(NARROW_FROM,'FROM public.channels_with_control c').replace("'is_hidden',coalesce(control.is_hidden,true),'hidden_at',control.hidden_at","'is_hidden',c.is_hidden,'hidden_at',c.hidden_at")


def view(schema,name):
    match=re.search(r'CREATE VIEW public\.'+name+r' WITH \([^\n]+\) AS\n(.*?);',schema,re.S)
    if match is None:raise AssertionError('view source changed')
    return match.group(1)


def from_clause(definition):
    return re.search(r'\n\s+FROM (.*)\Z',definition,re.S).group(1)


def projection(statement):
    return re.search(r"(SELECT jsonb_build_object\('channel_id',c\.channel_id,.*? AS value FROM .*?)\) projected",statement).group(1)


class ExportProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.schema=(ROOT/'schema/schema.sql').read_text(encoding='utf-8')

    def test_actual_view_topology_has_no_filters_and_unique_control_job_keys(self):
        outer=view(self.schema,'channels_with_control');inner=view(self.schema,'channel_collection_control')
        self.assertIn('COALESCE(control.is_hidden, true) AS is_hidden',outer)
        self.assertIn('control.hidden_at',outer);self.assertIn('channel.user_work_id',outer)
        self.assertNotIn('control.user_work_id',outer)
        self.assertIn('LEFT JOIN public.channel_collection_control control ON ((control.channel_id = channel.channel_id))',from_clause(outer))
        self.assertIn('JOIN public.channels channel ON ((channel.channel_id = control.channel_id))',from_clause(inner))
        self.assertIn('LEFT JOIN public.operator_jobs job ON ((job.id = control.initial_job_id))',from_clause(inner))
        for definition in (outer,inner):self.assertNotRegex(from_clause(definition),r'\b(WHERE|GROUP BY|HAVING|LIMIT|UNION)\b')
        for table,column in (('channels','channel_id'),('channel_collection_state','channel_id'),('operator_jobs','id')):
            self.assertRegex(self.schema,r'ALTER TABLE ONLY public\.'+table+r'\s+ADD CONSTRAINT \w+ PRIMARY KEY \('+column+r'\);')

    def test_only_three_reviewed_substitutions_change_exact_prior_sql(self):
        for name,expected in BEFORE_HASHES.items():
            statement=getattr(q,name)
            projection_statement=pre59_document(statement) if name=='_EXPORT_DOCUMENT' else statement
            prior_projection=previous(projection_statement)
            if name=='_EXPORT_DOCUMENT':
                # Encoding is independently guarded by test_read_structural_optimization.
                prior_projection=prior_projection.split(') AS value),',1)[0]+""") AS value)
SELECT octet_length(value::text) AS total_bytes,
 CASE WHEN octet_length(value::text) <= %(max_bytes)s::bigint THEN value ELSE NULL END AS value
FROM document
"""
            self.assertEqual(hashlib.sha256(prior_projection.encode()).hexdigest(),expected,name)
            self.assertNotIn('channels_with_control',statement)
            self.assertNotIn('operator_jobs',statement)
        for statement in (q._EXPORT_ESTIMATE,q._EXPORT_DOCUMENT):
            self.assertEqual(before_reader_views(statement).count(NARROW_FROM),1)
            self.assertEqual(statement.count("'is_hidden',coalesce(control.is_hidden,true),'hidden_at',control.hidden_at"),1)
        self.assertIn('SELECT 1 FROM readable.channel_user_work LIMIT 10001',q._EXPORT_CAPACITY)

    def database(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        db.create_function('jsonb_build_object',-1,lambda *a:json.dumps(dict(zip(a[::2],a[1::2])),ensure_ascii=False))
        db.executescript('''
          CREATE TABLE channels(channel_id TEXT PRIMARY KEY,user_work_id TEXT,user_input TEXT,folder_id TEXT,parent_channel_id TEXT,added_at TEXT);
          CREATE TABLE channel_collection_state(channel_id TEXT PRIMARY KEY,is_hidden INTEGER,hidden_at TEXT,initial_job_id TEXT);
          CREATE TABLE operator_jobs(id TEXT PRIMARY KEY);
        ''')
        inner=from_clause(view(self.schema,'channel_collection_control')).replace('public.','')
        outer=from_clause(view(self.schema,'channels_with_control')).replace('public.','')
        db.execute('CREATE VIEW channel_collection_control AS SELECT control.channel_id,control.is_hidden,control.hidden_at FROM '+inner)
        db.execute('CREATE VIEW channels_with_control AS SELECT '+','.join('channel.'+field for field in FIELDS if field not in ('is_hidden','hidden_at'))+',coalesce(control.is_hidden,true) AS is_hidden,control.hidden_at FROM '+outer)
        db.executemany('INSERT INTO channels VALUES(?,?,?,?,?,?)',[
          ('missing','w3',"  O'Brien\tÐšÐ¸Ñ—Ð²\n  ",'folder','visible','2026-01-01T00:00:00.123450+00:00'),
          ('visible','w1','  kept  ',None,None,None),('hidden','w4',None,None,None,'clock'),('dangling_job','w2','\t',None,None,None)])
        db.executemany('INSERT INTO channel_collection_state VALUES(?,?,?,?)',[
          ('visible',0,None,'job'),('hidden',1,'hidden-clock','job'),('dangling_job',0,None,'missing-job'),('orphan',1,'irrelevant',None)])
        db.execute("INSERT INTO operator_jobs VALUES('job')")
        return db

    def test_projected_values_match_retained_join_for_absent_hidden_and_job_variants(self):
        db=self.database()
        for statement in (q._EXPORT_ESTIMATE,pre59_document(q._EXPORT_DOCUMENT)):
            old=projection(previous(statement)).replace('public.','')
            new=projection(before_reader_views(statement)).replace('public.','')
            old_rows=sorted((json.loads(row[0]) for row in db.execute(old)),key=lambda r:r['user_work_id'])
            new_rows=sorted((json.loads(row[0]) for row in db.execute(new)),key=lambda r:r['user_work_id'])
            self.assertEqual(old_rows,new_rows)
            by_id={row['channel_id']:row for row in new_rows}
            self.assertEqual(set(by_id),{'missing','visible','hidden','dangling_job'})
            self.assertEqual(set(by_id['missing']),set(FIELDS))
            self.assertEqual(by_id['missing']['is_hidden'],1);self.assertIsNone(by_id['missing']['hidden_at'])
            self.assertEqual(by_id['missing']['user_input'],"  O'Brien\tÐšÐ¸Ñ—Ð²\n  ")
            self.assertEqual(by_id['missing']['added_at'],'2026-01-01T00:00:00.123450+00:00')
            self.assertEqual(by_id['hidden']['hidden_at'],'hidden-clock')
            self.assertEqual(by_id['dangling_job']['is_hidden'],0)

    def test_count_projection_has_identical_cardinality_with_missing_control(self):
        db=self.database()
        self.assertEqual(db.execute('SELECT count(*) FROM(SELECT 1 FROM channels LIMIT 10001)').fetchone(),(4,))
        self.assertEqual(db.execute('SELECT count(*) FROM(SELECT 1 FROM channels_with_control LIMIT 10001)').fetchone(),(4,))
        with self.assertRaises(sqlite3.IntegrityError):db.execute("INSERT INTO channel_collection_state VALUES('visible',1,NULL,NULL)")

    def test_estimate_and_document_use_identical_channel_projection(self):
        self.assertEqual(projection(before_reader_views(q._EXPORT_ESTIMATE)),projection(pre59_document(q._EXPORT_DOCUMENT)))
        self.assertIn("jsonb_agg(value ORDER BY value->>'user_work_id')",q._EXPORT_DOCUMENT)
        self.assertIn('octet_length(value::text)+2',q._EXPORT_ESTIMATE)
        self.assertIn('case when total_bytes <= %(max_bytes)s::bigint then payload else null end',q._EXPORT_DOCUMENT.lower())
        for statement in (q._EXPORT_CAPACITY,q._EXPORT_ESTIMATE,q._EXPORT_DOCUMENT):_reviewed_statement(statement,read=True,max_rows=1)

    def test_three_stages_and_single_repeatable_read_preserve_whitespace_values(self):
        document={key:[] for key in ('folders','labels','channels','assignments','saved_objects','members','pending','reviews')}
        document['channels']=[dict(channel_id='missing',user_work_id='id',user_input=' \t\n ',folder_id=None,parent_channel_id=None,is_hidden=True,hidden_at=None,added_at=None)]
        db=Database(COUNTS,{'total_bytes':100},{'total_bytes':200,'encoded_value':json.dumps(document)})
        self.assertEqual(q.export_snapshot(db),document)
        self.assertEqual([item[0] for item in db.queries],[q._EXPORT_CAPACITY,q._EXPORT_ESTIMATE,q._EXPORT_DOCUMENT])
        self.assertEqual(db.events,['begin','commit']);self.assertEqual(len(db.operations),1)
        self.assertTrue(db.operations[0].read_only and db.operations[0].repeatable_read)
        self.assertEqual(db.queries[-1][1],{'max_bytes':16777216})


if __name__=='__main__':unittest.main()
