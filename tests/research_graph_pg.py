"""Research graph execution and Saved/backup acceptance on disposable PG17."""
import sys,unittest,json,uuid
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'application'),str(Path(__file__).resolve().parent)]
import research_query_pg as native
from test_research_graph import graph_document
from research_query.validation import canonical,QueryRejected
from research_query.saved import from_query
from saved_views import SavedViewsService
from saved_work import SavedWorkError
from db.user_work.saved_views import SavedViewsRepository

class GraphPostgres(unittest.TestCase):
    setUpClass=classmethod(native.NativeQueryTests.setUpClass.__func__)
    tearDownClass=classmethod(native.NativeQueryTests.tearDownClass.__func__)
    refresh=classmethod(native.NativeQueryTests.refresh.__func__)
    def test_01_same_page_and_operation_as_direct_api(self):
        d=graph_document();d['graph']['nodes'].append(dict(id='summary',type='summary',parameters={}))
        d['graph']['edges'].append(dict(**{'from':'source','to':'summary'},input='query',output='query'));d['graph']['layout']['summary']=dict(x=300,y=300)
        direct=self.service.execute(canonical(d['query']))
        result=self.service.execute_graph(canonical(d))
        self.assertEqual(result['status'],'ready');self.assertEqual(result['nodes']['result']['value']['rows'],direct['rows'])
        direct_summary=self.service.execute_operation(canonical(dict(version=1,operation='video.summary.v1',query=d['query'],parameters={})))
        self.assertEqual(result['nodes']['summary']['value']['result'],direct_summary['result'])
        self.assertNotIn('rows',result['nodes']['source']);self.assertNotIn('notes',result)
    def test_02_failed_block_is_not_an_empty_success(self):
        d=graph_document('summary')
        with patch.object(self.repo,'read_operation',side_effect=QueryRejected('query_budget_exceeded')):
            result=self.service.execute_graph(canonical(d))
        self.assertEqual(result['status'],'partial');self.assertEqual(result['nodes']['result']['state'],'error')
        self.assertNotIn('value',result['nodes']['result'])
    def test_03_scope_fence_and_cancellation(self):
        d=graph_document()
        with self.assertRaisesRegex(QueryRejected,'query_budget_exceeded'):self.service.execute_graph(canonical(d),cancelled=lambda:True)
        original=self.service.execute
        identity=native.FIXTURE['channels'][0]['id']
        def changed(*args,**kwargs):
            value=original(*args,**kwargs)
            self.admin.execute('UPDATE public.channel_collection_state SET is_hidden=true WHERE channel_id=%s',(identity,))
            return value
        try:
            with patch.object(self.service,'execute',side_effect=changed),self.assertRaisesRegex(QueryRejected,'cursor_context_changed'):
                self.service.execute_graph(canonical(d))
        finally:self.admin.execute('UPDATE public.channel_collection_state SET is_hidden=false WHERE channel_id=%s',(identity,))

    def test_04_summary_and_calendar_keep_the_filter_blocks_age_limit(self):
        d=graph_document('summary')
        g=d['graph']
        # The condition lives in a graph block, not in the catalogue's columns.
        g['nodes'].insert(1,dict(id='condition',type='filter',parameters={'where':
            {'not':{'field':'observation.views_delta','op':'lt','value':'-1000000'}}}))
        g['nodes'].append(dict(id='calendar',type='calendar',parameters=dict(year=2026,time_zone='UTC')))
        g['edges']=[dict(**{'from':a,'to':b},input='query',output='query')
                    for a,b in [('source','condition'),('condition','result'),('condition','calendar')]]
        g['layout'].update(condition=dict(x=200,y=30),calendar=dict(x=500,y=300))
        result=self.service.execute_graph(canonical(d))
        self.assertEqual(result['status'],'ready')
        for block in ('result','calendar'):
            self.assertEqual(result['nodes'][block]['output'],'operation')
            self.assertEqual(result['nodes'][block]['value']['context']['observation_age_limit_seconds'],604800)
    def test_80_study_with_deleted_folder_moves_to_another_source(self):
        # Delete the source, open, choose another, save, open again.
        gone,kept=str(uuid.uuid4()),str(uuid.uuid4())
        for folder in (gone,kept):self.admin.execute("INSERT INTO public.folders(id,name) VALUES(%s,'Study folder')",(folder,))
        d=graph_document();d['query']['scope']={'kind':'folder','id':gone,'include_descendants':False}
        source,definition=from_query(d['query']);definition={**definition,'version':3,'graph':d['graph']}
        definition['graph']['notes']='Question and evidence links';definition['graph']['layout']['result']['x']=777
        service=SavedViewsService(SavedViewsRepository(self.db));identity=str(uuid.uuid4())
        try:
            created=service.create(dict(id=identity,name='Moving study',channel_id=source,conditions=definition))['view']
            self.admin.execute('DELETE FROM public.folders WHERE id=%s',(gone,))
            opened=service.get(identity)['view'];self.assertFalse(opened['channel_available'])
            moved=deepcopy(opened['conditions']);moved['query']['scope']['id']=kept
            saved=service.rename(identity,dict(name='Moving study',conditions=moved,expected_revision=created['revision']))['view']
            again=service.get(identity)['view']
            self.assertTrue(again['channel_available']);self.assertEqual(again['revision'],created['revision']+1)
            self.assertEqual(again['conditions']['graph'],definition['graph']);self.assertEqual(again['conditions']['query']['scope']['id'],kept)
            self.assertEqual(saved['id'],identity)
        finally:
            self.admin.execute('DELETE FROM public.saved_objects WHERE id=%s',(identity,))
            self.admin.execute('DELETE FROM public.folders WHERE id=ANY(%s)',([gone,kept],))
    def test_90_saved_edit_replay_conflict_backup_roundtrip(self):
        from db.user_work.user_work_read_queries import export_snapshot
        from db.user_work.user_work_restore import import_user_work_backup
        from user_work_backup import build_backup,encode_backup,decode_backup,import_payload
        d=graph_document();source,definition=from_query(d['query']);definition={**definition,'version':3,'graph':d['graph']}
        service=SavedViewsService(SavedViewsRepository(self.db));identity=str(uuid.uuid4())
        command=dict(id=identity,name='My portable study',channel_id=source,conditions=definition)
        created=service.create(command)['view'];self.assertTrue(created['channel_available'])
        edited=deepcopy(created['conditions']);edited['graph']['notes']='My own conclusion and an evidence link.';edited['graph']['layout']['result']['x']=800
        body=dict(name='Edited study',conditions=edited,expected_revision=1)
        changed=service.rename(identity,body)['view'];self.assertEqual(changed['revision'],2)
        self.assertEqual(service.rename(identity,body)['view'],changed)
        conflict=deepcopy(body);conflict['conditions']['graph']['notes']='A stale edit'
        with self.assertRaises(SavedWorkError):service.rename(identity,conflict)
        self.assertEqual(service.get(identity)['view']['conditions']['graph']['notes'],edited['graph']['notes'])
        backup=build_backup(export_snapshot(self.db))['backup'];self.assertEqual(backup['version'],6)
        backup=decode_backup(encode_backup(backup));kept=next(q for q in backup['user_work']['queries'] if q['id']==identity)
        self.assertEqual(kept['conditions'],edited);self.assertNotIn('result',kept['conditions']['graph'])
        # Restore is intentionally allowed only into an empty installation.
        # This test owns an isolated cluster and all fixture rows.
        self.admin.execute('DELETE FROM public.saved_objects')
        self.admin.execute('DELETE FROM public.channels')
        self.admin.execute('DELETE FROM public.folders')
        self.admin.execute("DELETE FROM public.channel_labels WHERE label_key<>'monetized'")
        self.assertEqual(import_user_work_backup(self.db,import_payload(backup))['outcome'],'ready')
        restored=service.get(identity)['view'];self.assertEqual(restored['conditions'],edited)
        self.assertEqual(restored['name'],'Edited study')
    def test_91_canvas_saves_edits_keeps_drafts_and_survives_backup(self):
        # A canvas (conditions version 4): two sources and a kept draft, bound to no channel.
        from db.user_work.user_work_read_queries import export_snapshot
        from db.user_work.user_work_restore import import_user_work_backup
        from user_work_backup import build_backup,encode_backup,decode_backup,import_payload
        from test_research_graph_v2 import GALLERY,VIEWS,canvas,source_params
        from test_research_query_contract import query
        folder,channel=str(uuid.uuid4()),query()['scope']['id']
        self.admin.execute("INSERT INTO public.folders(id,name) VALUES(%s,'Canvas folder')",(folder,))
        graph=canvas([('videos','source',source_params(query())),('views','filter',{'where':VIEWS}),('cards','gallery',GALLERY),
                      ('videos_2','source',source_params(query(scope={'kind':'folder','id':folder,'include_descendants':False}))),('cards_2','gallery',GALLERY),
                      ('draft','sort',{'order_by':[{'field':'video.views','direction':'desc'}]})],[('videos','views'),('views','cards'),('videos_2','cards_2')])['graph']
        definition=dict(version=4,graph=graph);service=SavedViewsService(SavedViewsRepository(self.db));identity=str(uuid.uuid4())
        with self.assertRaises(SavedWorkError):service.create(dict(id=identity,name='My canvas',channel_id=channel,conditions=definition))
        created=service.create(dict(id=identity,name='My canvas',channel_id=None,conditions=definition))['view']
        self.assertEqual(created['conditions'],definition);self.assertIsNone(created['channel_id'])
        edited=deepcopy(definition);edited['graph']['notes']='Two sources side by side';edited['graph']['layout']['draft']['x']=900
        changed=service.rename(identity,dict(name='My canvas',conditions=edited,expected_revision=1))['view'];self.assertEqual(changed['revision'],2)
        # A study and a canvas never replace each other in place; the database refuses a canvas bound to a channel.
        _,study=from_query(graph_document()['query'])
        with self.assertRaises(SavedWorkError):
            service.rename(identity,dict(name='My canvas',conditions={**study,'version':3,'graph':graph_document()['graph']},expected_revision=2))
        with self.assertRaises(Exception):self.admin.execute('UPDATE public.saved_objects SET channel_id=%s WHERE id=%s',(channel,identity))
        backup=decode_backup(encode_backup(build_backup(export_snapshot(self.db))['backup']))
        kept=next(q for q in backup['user_work']['queries'] if q['id']==identity);self.assertEqual(kept['conditions'],edited)
        # test_90's restore left pending channel items; this test owns the isolated cluster too.
        for table in ('saved_objects','channels','folders','user_channel_restore_items'):self.admin.execute(f'DELETE FROM public.{table}')
        self.admin.execute("DELETE FROM public.channel_labels WHERE label_key<>'monetized'")
        self.assertEqual(import_user_work_backup(self.db,import_payload(backup))['outcome'],'ready')
        restored=service.get(identity)['view'];self.assertEqual(restored['conditions'],edited);self.assertEqual(restored['revision'],2)

if __name__=='__main__':unittest.main(verbosity=2)
