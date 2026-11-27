"""Research definitions are typed, bounded user work, not result snapshots."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json,sys,unittest
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from research_query.graph import validate_graph,normalize_saved_research
from research_query.validation import AccessContext,QueryRejected,canonical,validate
from research_query.operations import OPERATIONS
from research_query.saved import from_query
from test_research_query_contract import query

def graph_document(kind='table'):
    return dict(version=1,query=query(),graph=dict(version=1,
        nodes=[dict(id='source',type='source',parameters={}),dict(id='result',type=kind,parameters={})],
        edges=[dict(**{'from':'source','to':'result'},input='query',output='query')],notes='My research question',
        layout=dict(source=dict(x=20,y=30),result=dict(x=340,y=30))))

class GraphContractTests(unittest.TestCase):
    def setUp(self):self.context=AccessContext('synthetic','owner',enabled=True,allowed_operations=frozenset(OPERATIONS))
    def test_layout_and_notes_do_not_change_execution_identity(self):
        a=graph_document();b=deepcopy(a);b['graph']['layout']['result']['y']=600;b['graph']['notes']='Different user note'
        self.assertEqual(validate_graph(canonical(a),self.context).fingerprint,validate_graph(canonical(b),self.context).fingerprint)
    def test_saved_definition_no_results_or_cursor(self):
        a=graph_document();_,definition=from_query(a['query'])
        saved=normalize_saved_research(dict(version=3,query=definition['query'],graph=a['graph']))
        self.assertEqual(saved['graph']['notes'],a['graph']['notes']);self.assertEqual(saved['query']['scope']['id'],definition['query']['scope']['id'])
        for key in ('rows','result','data','code','sql'):
            bad=deepcopy(saved);bad['graph'][key]=[]
            with self.assertRaises(ValueError):normalize_saved_research(bad)
        saved['query']['page']['after']='opaque'
        with self.assertRaises(ValueError):normalize_saved_research(saved)
    def test_cycles_invalid_ports_grain_and_limit_refused(self):
        for mutation in (
            lambda d:d['graph']['edges'][0].update({'from':'result'}),
            lambda d:d['graph']['edges'][0].update(output='rows'),
            lambda d:d['graph']['nodes'][1].update(type='eval'),
            lambda d:d['graph']['nodes'][1].update(type='history',parameters=dict(metric='views',hours=24,bucket_hours=1)),
            lambda d:d['graph'].update(notes='x'*4097),
            lambda d:d['graph']['layout']['result'].update(x=True),
        ):
            bad=graph_document();mutation(bad)
            with self.assertRaises(QueryRejected):validate_graph(canonical(bad),self.context)
    def test_server_permissions_apply_to_whole_graph(self):
        with self.assertRaisesRegex(QueryRejected,'policy_denied'):
            validate_graph(canonical(graph_document('summary')),replace(self.context,allowed_operations=frozenset()))
        q=graph_document();q['query']['select'].append('video.description')
        with self.assertRaisesRegex(QueryRejected,'field_disabled'):
            validate_graph(canonical(q),replace(self.context,disabled_type_fields=frozenset({('long','video.description')})))
    def test_empty_filter_preserves_catalogue_predicate_and_page_size(self):
        a=graph_document();a['query'].update(where={'field':'video.views','op':'gte','value':'100'},page={'size':200})
        g=a['graph'];g['nodes'].insert(1,dict(id='filter',type='filter',parameters={'where':None}));g['layout']['filter']={'x':100,'y':50}
        g['edges']=[dict(**{'from':'source','to':'filter'},input='query',output='query'),dict(**{'from':'filter','to':'result'},input='query',output='query')]
        plan=validate_graph(canonical(a),self.context);result=json.loads(dict(plan.query_plans)['result'].encoded)
        self.assertEqual(result['where'],a['query']['where']);self.assertEqual(result['page']['size'],200)

    def test_future_fields_preserved_but_never_executed(self):
        a=graph_document('summary');_,definition=from_query(a['query'])
        definition['query']['select'].append('video.future_metric')
        saved=normalize_saved_research(dict(version=3,query=definition['query'],graph=a['graph']))
        self.assertIn('video.future_metric',saved['query']['select'])
        a['query']['select'].append('video.future_metric')
        with self.assertRaisesRegex(QueryRejected,'unsupported_field'):validate_graph(canonical(a),self.context)
        saved['graph']['nodes'][1]['parameters']={'sql':'select 1'}
        with self.assertRaises(ValueError):normalize_saved_research(saved)

    def test_selection_and_filter_compose_queries_before_pages(self):
        a=graph_document();g=a['graph'];g['nodes'].insert(1,dict(id='select',type='selection',parameters=dict(mode='include',ids=['a0000000001'])))
        g['layout']['select']=dict(x=180,y=30);g['edges']=[dict(**{'from':'source','to':'select'},input='query',output='query'),dict(**{'from':'select','to':'result'},input='query',output='query')]
        plan=validate_graph(canonical(a),self.context);q=json.loads(dict(plan.query_plans)['result'].encoded)
        self.assertEqual(q['where']['value'],['a0000000001'])


# Run options without a database. The fake repository serves
# the same 120 synthetic rows as a fixed list and as current pages, in id order;
# tests/research_graph_frozen_pg.py proves the same claims on PostgreSQL 17.
from datetime import timedelta
from time import monotonic
from unittest.mock import patch
from research_query.contracts import MAX_FROZEN_ROWS
from research_query.cursor import CursorCodec
from research_query.frozen import FrozenStore
from research_query.graph import fixed_row_blocks
from research_query.service import QueryService
from test_research_frozen import (CONTEXT as FROZEN_CONTEXT, NOW as FROZEN_NOW, Repository as FrozenRepository,
                                  frozen_query)

CONDITION={'field':'video.views','op':'gte','value':'100'}
CALENDAR_RESULT=dict(population=120,known_count=0,year=2026,time_zone='UTC',days=[],hours=[],weekdays=[])
# A row block fed directly, one behind a condition-free filter, one behind a condition, a calculation.
STUDY=(('fixed','table',{},'source'),('plain','filter',{'where':None},'source'),('fixed_gallery','gallery',{},'plain'),
       ('condition','filter',{'where':CONDITION},'source'),('live','table',{},'condition'),
       ('calendar','calendar',{'year':2026,'time_zone':'UTC'},'source'))


def study(blocks=STUDY,run=None,**changes):
    """A study over the synthetic channel: source plus (id, type, parameters, parent) blocks."""
    q=frozen_query(consistency='current');q.update(changes)
    nodes=[dict(id='source',type='source',parameters={})];edges=[];layout={'source':dict(x=0,y=0)}
    for index,(identity,kind,parameters,parent) in enumerate(blocks,1):
        nodes.append(dict(id=identity,type=kind,parameters=deepcopy(parameters)))
        edges.append({'from':parent,'to':identity,'input':'query','output':'query'})
        layout[identity]=dict(x=200*index,y=0)
    document=dict(version=1,query=q,graph=dict(version=1,nodes=nodes,edges=edges,notes='Why do these rows differ?',layout=layout))
    if run is not None:document['run']=run
    return document


class GraphRepository(FrozenRepository):
    """Fixed lists (read_frozen), current pages and calculations over the same rows."""
    def __init__(self):
        super().__init__()
        self.rows=deepcopy(self.data['rows']);self.current=self.operations=0;self.calls=[]
    def scope_revision(self,plan,*,deadline,cancelled):
        self.calls.append('scope_revision')
        return self.revision
    def read_frozen(self,plan,*,deadline,cancelled):
        self.calls.append('read_frozen')
        return super().read_frozen(plan,deadline=deadline,cancelled=cancelled)
    def recheck_scope(self,plan,revision,*,deadline,cancelled):
        self.calls.append('recheck_scope')
        return super().recheck_scope(plan,revision,deadline=deadline,cancelled=cancelled)
    def _current(self):
        data={k:v for k,v in self.data.items() if k not in {'rows','fresh_until','frozen_count','expiring_rows','scope_channels'}}
        return {**deepcopy(data),'valid_before':FROZEN_NOW+timedelta(seconds=120)}
    def read_page(self,plan,continuation,*,deadline,cancelled):
        self.calls.append('read_page');self.current+=1;self.on_read()
        ids=[row['video_id'] for row in self.rows]
        start=0 if continuation is None else ids.index(continuation['keys'][0])+1
        size=plan.request.page.size
        return {**self._current(),'rows':[dict(deepcopy(row),sort_keys=[row['video_id']]) for row in self.rows[start:start+size+1]]}
    def read_operation(self,plan,*,deadline,cancelled):
        self.calls.append('read_operation');self.operations+=1;self.on_read()
        return {**self._current(),'result':deepcopy(CALENDAR_RESULT)}


class FrozenRunTests(unittest.TestCase):
    def setUp(self):
        self.now,self.mono,self.context=FROZEN_NOW,1000.0,FROZEN_CONTEXT
        self.repo=GraphRepository()
        self.store=FrozenStore(clock=lambda:self.now,monotonic=lambda:self.mono,auto_prune=False)
        self.codec=CursorCodec()
        self.service=QueryService(self.repo,lambda:self.context,clock=lambda:self.now,codec=self.codec,frozen=self.store)

    def run_study(self,document):
        return self.service.execute_graph(canonical(document))

    def library(self,**changes):
        """Library's first page of its frozen list (the query route)."""
        q=frozen_query();q.update(changes)
        return self.service.execute(canonical(q))

    def rejected(self,code,call,path=None):
        with self.assertRaises(QueryRejected) as caught:call()
        self.assertEqual((caught.exception.code,caught.exception.path),(code,path))

    def session(self,token,reference=True):
        return self.codec.decode_frozen(token,reference=reference)

    def without_tokens(self,page):
        page=deepcopy(page);page['context'].pop('frozen_ref')
        for key in ('next_cursor','previous_cursor'):page['page'][key]=page['page'][key] is not None
        return page

    def same_page(self,page,expected):
        """Equal in everything but the opaque tokens, which name the same result and offsets."""
        self.assertEqual(self.without_tokens(page),self.without_tokens(expected))
        self.assertEqual(self.session(page['context']['frozen_ref']).session_id,self.session(expected['context']['frozen_ref']).session_id)
        for key in ('next_cursor','previous_cursor'):
            if expected['page'][key] is not None:
                ours,theirs=self.session(page['page'][key],False),self.session(expected['page'][key],False)
                self.assertEqual((ours.session_id,ours.offset),(theirs.session_id,theirs.offset))

    def test_run_options_are_exact_and_never_part_of_the_definition(self):
        base=validate_graph(canonical(study()),self.context)
        self.assertIsNone(base.run)
        for run in ({'consistency':'current'},{'consistency':'frozen'},{'consistency':'frozen','frozen_ref':'x'*16384}):
            plan=validate_graph(canonical(study(run=run)),self.context)
            self.assertEqual((plan.run.consistency,plan.run.frozen_ref),(run['consistency'],run.get('frozen_ref')))
            self.assertEqual((plan.fingerprint,plan.definition,plan.order),(base.fingerprint,base.definition,base.order))
        self.assertNotIn(b'"run"',base.definition)
        for run in (None,[],{},'frozen',{'consistency':'Frozen'},{'consistency':True},{'consistency':'current','frozen_ref':'x'},
                    {'consistency':'frozen','frozen_ref':''},{'consistency':'frozen','frozen_ref':'x'*16385},
                    {'consistency':'frozen','frozen_ref':7},{'consistency':'frozen','frozen_ref':None},
                    {'consistency':'frozen','after':'x'},{'frozen_ref':'x'}):
            with self.subTest(run=run):
                document=study();document['run']=run  # Explicit, also "run": null.
                self.rejected('invalid_query',lambda:validate_graph(canonical(document),self.context))
        # The definition itself stays current, and options are never saved user work.
        frozen=study(run={'consistency':'frozen'});frozen['query']['consistency']='frozen'
        self.rejected('invalid_query',lambda:validate_graph(canonical(frozen),self.context))
        a=graph_document();_,definition=from_query(a['query'])
        with self.assertRaises(ValueError):
            normalize_saved_research(dict(version=3,query=definition['query'],graph=a['graph'],run={'consistency':'frozen'}))

    def test_row_blocks_fed_directly_by_the_source_share_the_fixed_list(self):
        blocks=(('fixed','table',{},'source'),('plain','filter',{'where':None},'source'),('again','filter',{'where':None},'plain'),
                ('fixed_gallery','gallery',{},'again'),('picked','selection',{'mode':'include','ids':['a0000000001']},'source'),
                ('chosen','table',{},'picked'),('condition','filter',{'where':CONDITION},'source'),('live','gallery',{},'condition'))
        plan=validate_graph(canonical(study(blocks,run={'consistency':'frozen'})),self.context)
        root,fixed=fixed_row_blocks(plan,self.context)
        self.assertEqual(fixed,('fixed','fixed_gallery'))
        self.assertEqual(json.loads(root.encoded)['consistency'],'frozen')
        # The list is the query Library freezes for the same study query.
        self.assertEqual(root.fingerprint,validate(canonical(frozen_query()),self.context).fingerprint)
        plan=validate_graph(canonical(study(STUDY,run={'consistency':'frozen'})),self.context)
        self.assertEqual(fixed_row_blocks(plan,self.context)[1],('fixed','fixed_gallery'))

    def test_without_run_options_the_response_is_unchanged(self):
        result=self.run_study(study())
        self.assertEqual(set(result),{'version','graph_fingerprint','context','nodes','status','budgets','warnings'})
        self.assertEqual(result['context']['consistency'],'sequential_current_reads')
        self.assertEqual(result['warnings'],['Results are current reads, not a frozen historical snapshot.'])
        for identity in ('fixed','fixed_gallery','live','calendar'):
            self.assertEqual(set(result['nodes'][identity]),{'state','output','value','elapsed_ms'})
        self.assertEqual(result['nodes']['plain'],{'state':'ready','output':'query','grain':'video'})
        self.assertEqual(result['nodes']['fixed']['value']['context']['consistency'],'current')
        self.assertNotIn('read_frozen',self.repo.calls)
        self.assertEqual((self.repo.current,self.repo.operations),(3,1))

    def test_fixed_run_shows_library_list_in_every_direct_row_block(self):
        library=self.library()
        reads=self.repo.reads
        result=self.run_study(study(run={'consistency':'frozen','frozen_ref':library['context']['frozen_ref']}))
        self.assertEqual(self.repo.reads,reads)  # No new read of rows for the list.
        self.assertEqual(result['status'],'ready')
        self.assertEqual(result['context']['consistency'],'fixed_source_list')
        self.assertEqual(result['run'],{'consistency':'frozen','fixed':{'reused':True,'evaluated_at':library['context']['evaluated_at'],
                                         'valid_before':library['context']['valid_before'],'total_count':120}})
        nodes=result['nodes']
        for identity in ('fixed','fixed_gallery'):
            self.assertEqual(nodes[identity]['consistency'],'frozen')
            self.same_page(nodes[identity]['value'],library)
            # The block's paging query is Library's query (the same fingerprint), without a continuation.
            self.assertEqual(nodes[identity]['query']['page'],{'size':50})
            self.assertEqual(validate(canonical(nodes[identity]['query']),self.context).fingerprint,library['query_fingerprint'])
        for identity,output in (('live','page'),('calendar','operation')):
            self.assertEqual((nodes[identity]['consistency'],nodes[identity]['output']),('current',output))
            self.assertEqual(nodes[identity]['value']['context']['consistency'],'current')
        self.assertEqual(nodes['live']['query']['where'],CONDITION)
        self.assertNotIn('query',nodes['calendar'])
        # Each block keeps its own lifetime; the run's is the earliest.
        self.assertEqual(nodes['fixed']['value']['context']['valid_before'],'2026-10-29T12:10:00Z')
        self.assertEqual(nodes['live']['value']['context']['valid_before'],'2026-10-29T12:02:00Z')
        self.assertEqual(result['context']['valid_before'],'2026-10-29T12:02:00Z')
        # The block pages through the query route with its own query, like Library.
        block=nodes['fixed']['value']
        following=self.service.execute(canonical({**nodes['fixed']['query'],'page':{'size':50,'after':block['page']['next_cursor']}}))
        expected=self.service.execute(canonical({**frozen_query(),'page':{'size':50,'after':library['page']['next_cursor']}}))
        self.same_page(following,expected)
        self.assertEqual(following['page']['start_index'],50)

    def test_fixed_run_without_reference_fixes_one_list_for_all_row_blocks(self):
        result=self.run_study(study(run={'consistency':'frozen'}))
        self.assertEqual((self.repo.reads,self.store.usage()['sessions']),(1,1))
        fixed,gallery=result['nodes']['fixed']['value'],result['nodes']['fixed_gallery']['value']
        self.same_page(gallery,fixed)
        self.assertEqual(result['run']['fixed']['reused'],False)
        self.assertEqual(result['run']['fixed']['total_count'],120)
        self.assertEqual(fixed['context']['valid_before'],result['run']['fixed']['valid_before'])
        # The new list is a list like Library's: it serves the query route and a next run.
        again=self.run_study(study(run={'consistency':'frozen','frozen_ref':fixed['context']['frozen_ref']}))
        self.assertEqual((self.repo.reads,again['run']['fixed']['reused']),(1,True))
        self.same_page(again['nodes']['fixed']['value'],fixed)
        self.assertEqual(self.run_study(study(run={'consistency':'frozen'}))['run']['fixed']['reused'],False)
        self.assertEqual(self.repo.reads,2)

    def test_reference_refusals_refuse_the_whole_run(self):
        def refused(code,run_ref,**changes):
            reads=self.repo.reads
            self.rejected(code,lambda:self.run_study(study(run={'consistency':'frozen','frozen_ref':run_ref},**changes)))
            self.assertEqual(self.repo.reads,reads)  # Never silently replaced by a new list.
        library=self.library();ref=library['context']['frozen_ref']
        for token in ('not-a-reference',ref[:-2]+('AA' if ref[-2:]!='AA' else 'BB'),library['page']['next_cursor']):
            refused('cursor_expired',token)
        # Another query: its list stays valid for that query only.
        for changes in ({'order_by':[{'field':'video.title','direction':'asc','nulls':'last'}]},{'page':{'size':25}},
                        {'time':{'mode':'current','timezone':'Europe/Moscow'}},{'where':CONDITION},{'select':['video.id','video.title']}):
            with self.subTest(changes=changes):
                refused('cursor_context_changed',ref,**changes)
        self.assertEqual(self.service.execute(canonical({**frozen_query(),'page':{'size':50,'after':library['page']['next_cursor']}}))['page']['start_index'],50)
        # Expired by the clock, after a restart (a new key), or invalidated.
        self.now,self.mono=FROZEN_NOW+timedelta(seconds=600),self.mono+600
        refused('cursor_expired',ref)
        self.now,self.mono=FROZEN_NOW,self.mono+1
        ref=self.library()['context']['frozen_ref']
        restarted=QueryService(self.repo,lambda:self.context,clock=lambda:self.now,codec=CursorCodec(),frozen=self.store)
        self.rejected('cursor_expired',lambda:restarted.execute_graph(canonical(study(run={'consistency':'frozen','frozen_ref':ref}))))
        self.store.invalidate_channel(frozen_query()['scope']['id'])
        refused('cursor_expired',ref)
        # Changed access or scope: the list can never be served again.
        ref=self.library()['context']['frozen_ref']
        self.context=replace(FROZEN_CONTEXT,access_revision='owner-v2')
        refused('cursor_context_changed',ref)
        self.context=FROZEN_CONTEXT
        refused('cursor_expired',ref)
        ref=self.library()['context']['frozen_ref']
        self.repo.revision='b'*64
        refused('cursor_context_changed',ref)

    def test_a_list_that_cannot_be_fixed_is_an_error_of_its_blocks_only(self):
        gate = dict(deadline=monotonic() + 2, cancelled=lambda: False)
        with self.store.build_slot(**gate), self.store.build_slot(**gate), patch('research_query.frozen_service.FROZEN_BUILD_QUEUE_SECONDS', .03):
            result=self.run_study(study(run={'consistency':'frozen'}))
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['context']['consistency'],'sequential_current_reads')
        self.assertEqual(result['run'],{'consistency':'frozen','fixed':None})
        for identity in ('fixed','fixed_gallery'):
            node=result['nodes'][identity]
            self.assertEqual({k:v for k,v in node.items() if k!='elapsed_ms'},{'state':'error','code':'query_busy','consistency':'frozen'})
        self.assertEqual((result['nodes']['live']['state'],result['nodes']['calendar']['state']),('ready','ready'))
        self.repo.data.update(frozen_count=MAX_FROZEN_ROWS+1,rows=None)
        result=self.run_study(study(run={'consistency':'frozen'}))
        self.assertEqual({(n['code'],n['path'],n.get('limit')) for n in (result['nodes']['fixed'],result['nodes']['fixed_gallery'])},
                         {('query_budget_exceeded','consistency','rows')}) # the bound it reached
        self.assertEqual(self.repo.reads,1)  # One attempt for both blocks.
        self.assertEqual(result['nodes']['live']['state'],'ready')

    def test_queued_frozen_build_respects_the_canvas_deadline_and_cancellation(self):
        body = canonical(study(run={'consistency':'frozen'}))
        gate = dict(deadline=monotonic() + 3, cancelled=lambda: False)
        stopped = False
        def cancel_when_queued():
            nonlocal stopped
            stopped = stopped or bool(self.store._builds._waiting)
            return stopped
        with self.store.build_slot(**gate), self.store.build_slot(**gate):
            with patch('research_query.graph_service.GRAPH_SECONDS', .12), self.assertRaises(QueryRejected) as caught:
                self.service.execute_graph(body)
            self.assertEqual(caught.exception.payload(), {'code':'query_budget_exceeded','path':None,'limit':'time'})
            with self.assertRaises(QueryRejected) as caught:
                self.service.execute_graph(body, cancelled=cancel_when_queued)
            self.assertTrue(stopped)
            self.assertEqual(caught.exception.payload(), {'code':'query_budget_exceeded','path':None})
        self.assertEqual(self.repo.reads, 0)
        self.assertEqual(self.store.usage()['sessions'], 0)

    def test_a_list_lost_before_the_answer_refuses_the_run(self):
        ref=self.library()['context']['frozen_ref']
        self.repo.on_read=self.store.invalidate_all  # During a later current block.
        self.rejected('cursor_expired',lambda:self.run_study(study(run={'consistency':'frozen','frozen_ref':ref})))
        self.repo.on_read=lambda:None
        ref=self.library()['context']['frozen_ref']
        self.repo.on_read=lambda:setattr(self,'now',FROZEN_NOW+timedelta(seconds=601))
        self.rejected('cursor_expired',lambda:self.run_study(study(run={'consistency':'frozen','frozen_ref':ref})))

    def test_frozen_run_without_a_directly_fed_row_block_fixes_nothing(self):
        blocks=(('condition','filter',{'where':CONDITION},'source'),('live','table',{},'condition'),
                ('calendar','calendar',{'year':2026,'time_zone':'UTC'},'source'))
        for run in ({'consistency':'frozen'},{'consistency':'frozen','frozen_ref':'not-a-reference'}):
            result=self.run_study(study(blocks,run=run))
            self.assertEqual(result['run'],{'consistency':'frozen','fixed':None})
            self.assertEqual(result['context']['consistency'],'sequential_current_reads')
            self.assertEqual({result['nodes'][i]['consistency'] for i in ('live','calendar')},{'current'})
        self.assertEqual((self.repo.reads,self.store.usage()['sessions']),(0,0))

    def test_current_run_options_label_blocks_and_return_their_paging_query(self):
        result=self.run_study(study(run={'consistency':'current'}))
        self.assertEqual(result['run'],{'consistency':'current','fixed':None})
        self.assertEqual(result['context']['consistency'],'sequential_current_reads')
        self.assertEqual({result['nodes'][i]['consistency'] for i in ('fixed','fixed_gallery','live','calendar')},{'current'})
        node=result['nodes']['fixed']
        self.assertEqual(node['query']['consistency'],'current')
        self.assertNotIn('after',node['query']['page'])
        following=self.service.execute(canonical({**node['query'],'page':{'size':50,'after':node['value']['page']['next_cursor']}}))
        self.assertEqual(following['rows'][0]['id'],'a0000000050')
        self.assertNotIn('read_frozen',self.repo.calls)

    def test_runs_read_only_stored_data(self):
        import ast
        from pathlib import Path
        root=Path(__file__).resolve().parents[1]/'application/research_query'
        allowed={'copy','dataclasses','datetime','hashlib','json','re','secrets','time','types','pydantic'}
        for name in ('graph.py','graph_service.py','frozen_service.py'):
            for node in ast.walk(ast.parse((root/name).read_text(encoding='utf-8'))):
                if isinstance(node,ast.Import):
                    self.assertTrue({a.name.split('.')[0] for a in node.names}<=allowed,(name,ast.dump(node)))
                if isinstance(node,ast.ImportFrom) and node.level==0:
                    self.assertIn(node.module.split('.')[0],allowed|{'contextlib'},name)
        ref=self.library()['context']['frozen_ref']
        for run in ({'consistency':'frozen'},{'consistency':'frozen','frozen_ref':ref},{'consistency':'current'},None):
            self.run_study(study(run=run))
        self.assertEqual(set(self.repo.calls),{'scope_revision','read_frozen','recheck_scope','read_page','read_operation'})

    def test_http_route_takes_run_options_and_answers_refusals(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from research_query.http import create_router
        app=FastAPI();app.include_router(create_router(lambda request:self.service,lambda:None))
        with TestClient(app) as client:
            library=client.post('/research/queries/execute',json=frozen_query()).json()
            run={'consistency':'frozen','frozen_ref':library['context']['frozen_ref']}
            answer=client.post('/research/graphs/execute',json=study(run=run))
            self.assertEqual(answer.status_code,200,answer.text)
            self.assertEqual(answer.json()['nodes']['fixed']['consistency'],'frozen')
            checked=client.post('/research/graphs/validate',json=study(run=run))
            self.assertEqual(checked.json()['graph_fingerprint'],client.post('/research/graphs/validate',json=study()).json()['graph_fingerprint'])
            bad=client.post('/research/graphs/execute',json=study(run={'consistency':'frozen','frozen_ref':''}))
            self.assertEqual((bad.status_code,bad.json()),(422,{'code':'invalid_query','path':None}))
            self.now,self.mono=FROZEN_NOW+timedelta(seconds=601),self.mono+601
            gone=client.post('/research/graphs/execute',json=study(run=run))
            self.assertEqual((gone.status_code,gone.json()),(409,{'code':'cursor_expired','path':None}))

if __name__=='__main__':unittest.main()
