"""Agent limits are server policies, not adapter hints or per-token quotas."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from research_query.admission import ReadAdmission, LimitedReadAdmission
from research_query.aggregate import validate_operation
from research_query.discovery import contracts
from research_query.graph import validate_graph
from research_query.validation import AccessContext, QueryRejected, canonical, capabilities, validate
from research_query.operations import OPERATIONS
from test_research_query_contract import query


class AgentReadLimitsTests(unittest.TestCase):
    def test_current_only_applies_to_queries_graphs_and_operation_populations(self):
        owner = AccessContext('installation', 'owner', enabled=True, allowed_operations=frozenset(OPERATIONS))
        agent = replace(owner, access_revision='agent', allow_frozen=False)
        q = query(consistency='frozen')
        validate(canonical(q), owner)
        with self.assertRaisesRegex(QueryRejected, 'policy_denied'):
            validate(canonical(q), agent)
        for version in (1, 2):
            if version == 2:
                graph = contracts(owner, 'canvas')['example']
            else:
                graph = {'version':1, 'query':query(), 'graph':{'version':1,'nodes':[
                    {'id':'s','type':'source','parameters':{}},{'id':'t','type':'table','parameters':{}}],
                    'edges':[{'from':'s','to':'t','output':'query','input':'query'}],
                    'notes':'','layout':{'s':{'x':0,'y':0},'t':{'x':1,'y':0}}}}
            for run in ({'consistency':'frozen'}, {'consistency':'frozen','frozen_ref':'synthetic'}):
                graph['run'] = run
                validate_graph(canonical(graph), owner)
                with self.assertRaisesRegex(QueryRejected, 'policy_denied'):
                    validate_graph(canonical(graph), agent)
        for operation, params in [('video.facets.v1', {}), ('video.static_details.v1', {'video_id':'a0000000001'})]:
            body = {'version':1,'operation':operation,'query':query(), 'parameters':{
                **params,'population':{'kind':'frozen','frozen_ref':'synthetic'}}}
            validate_operation(canonical(body), owner)
            with self.assertRaisesRegex(QueryRejected, 'policy_denied'):
                validate_operation(canonical(body), agent)
        self.assertEqual(capabilities(agent)['consistency_modes'], ['current'])
        self.assertEqual(contracts(agent)['schema']['properties']['consistency']['enum'], ['current'])
        self.assertEqual(contracts(agent, 'canvas')['consistency_modes'], ['current'])

    def test_waiting_agent_does_not_occupy_owner_capacity_and_cancellation_releases(self):
        shared = ReadAdmission(concurrency=2)
        agents = LimitedReadAdmission(shared, concurrency=1)
        cancel = threading.Event()
        def read():
            with agents.enter(deadline=time.monotonic()+2, cancelled=cancel.is_set):
                self.fail('a second agent entered before the first left')
        with ThreadPoolExecutor(max_workers=1) as pool:
            with agents.enter(deadline=time.monotonic()+2, cancelled=lambda:False):
                future = pool.submit(read)
                until = time.monotonic()+1
                while time.monotonic()<until:
                    with agents.group._condition:
                        if agents.group._waiting: break
                    time.sleep(.001)
                self.assertEqual(len(agents.group._waiting), 1)
                self.assertEqual(len(shared._waiting), 0)
                with shared.enter(deadline=time.monotonic()+.2, cancelled=lambda:False):
                    self.assertEqual(shared._active, 2)
                cancel.set()
                with self.assertRaisesRegex(QueryRejected, 'query_budget_exceeded'): future.result(1)
        self.assertEqual((shared._active, agents.group._active), (0, 0))
        # If the shared queue expires, the agent lane is released too.
        with shared.enter(deadline=time.monotonic()+1, cancelled=lambda:False):
            with shared.enter(deadline=time.monotonic()+1, cancelled=lambda:False):
                with self.assertRaisesRegex(QueryRejected, 'query_busy'):
                    with agents.enter(deadline=time.monotonic()+.02, cancelled=lambda:False): pass
        self.assertEqual((shared._active, agents.group._active), (0, 0))


if __name__ == '__main__': unittest.main()
