"""Owner alert delivery against a freshly created disposable PostgreSQL 17."""
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'application'),str(ROOT/'tests')]
import research_query_pg as fixture
from alerts import AlertRule
from alert_worker import AlertWorker
from db.user_work.alerts import AlertsRepository
from db.base.postgres import PostgresConfig, PostgresDatabase
from db.base.read_context import http_read_scope
from collection_schedule import CollectionSchedules
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from research_query.service import QueryService
from research_query.validation import AccessContext
from db.reads.research_query import ResearchQueryRepository
from web.alerts import create_alert_router
import psycopg


class AlertDelivery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE') != '1':
            raise RuntimeError('disposable_cluster_required')
        port=int(os.environ['LIC_QUERY_TEST_PORT'])
        password=os.environ['LIC_QUERY_TEST_PASSWORD']
        params=dict(host='127.0.0.1',port=port,dbname='leti-cook',password=password)
        cls.admin=psycopg.connect(**params,user='postgres',autocommit=True)
        assert int(cls.admin.execute('SHOW server_version_num').fetchone()[0])//10000==17
        fixture.bootstrap(cls.admin)
        now=cls.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        fixture.seed(cls.admin,now-timedelta(seconds=10))
        dsn=psycopg.conninfo.make_conninfo(**params,user='authenticator')
        cls.db=PostgresDatabase(PostgresConfig(dsn),cadence_snapshot=CollectionSchedules().cadence_snapshot(),
                                admission=lambda _:None).open()
        cls.repo=AlertsRepository(cls.db)
        cls.channel=fixture.FIXTURE['channels'][0]['id']
        cls.video='a0000000001'
        app=FastAPI()
        def owner(request:Request):
            if request.headers.get('Authorization')!='test-owner':
                raise HTTPException(401)
        svc=QueryService(ResearchQueryRepository(cls.db),lambda:AccessContext("test","owner",enabled=True))
        app.include_router(create_alert_router(cls.db,lambda _:svc,owner))
        cls.client=TestClient(app,headers={'Authorization':'test-owner'})
        cls.addClassCleanup(cls.client.close)
        cls.addClassCleanup(cls.db.close)
        cls.addClassCleanup(cls.admin.close)

    def setUp(self):
        self.admin.execute("DELETE FROM public.alert_observations")
        self.admin.execute("DELETE FROM public.alert_rules")
        self.admin.execute("DELETE FROM public.video_snapshots WHERE video_id=ANY(%s)",([self.video,'s0000000001'],))

    def add(self,event='video.views',direction='up',scope=None,rule_id=None,**extra):
        rule={'event':event,'scope':scope or {'kind':'channel','id':self.channel},'name':event}
        if event!='channel.subscribers':
            rule['video_type']='long'
        if event in ('video.views','video.likes','video.comments','channel.subscribers'):
            rule.update(threshold='100000',direction=direction)
        rule.update(extra)
        r=self.client.post('/alerts/rules',json={'id':str(rule_id or uuid4()),'rule':rule})
        self.assertEqual(r.status_code,201,r.text)
        return r.json()

    def snapshot(self,views,likes=None,comments=None,epoch='test.views',video=None):
        return self.admin.execute("""INSERT INTO public.video_snapshots(video_id,views,like_count,comment_count,
          recorded_at,views_definition_epoch) VALUES(%s,%s,%s,%s,clock_timestamp(),%s) RETURNING id""",
          (video or self.video,views,likes,comments,epoch)).fetchone()[0]

    def messages(self):
        with http_read_scope():
            return self.repo.inbox(2**63-1,('long','short'))

    def test_crossings_and_restart(self):
        self.snapshot(99000)
        up=self.add()
        down=self.add(direction='down')
        for count in (100000,103000,98000):
            self.snapshot(count)
        self.assertFalse(self.messages())
        self.assertGreater(AlertsRepository(self.db).process(('long','short')),0)
        self.assertEqual(self.repo.process(('long','short')),0)
        items=self.messages()
        self.assertEqual([(i['direction'],i['previous_value'],i['current_value']) for i in reversed(items)],
                         [('up','99000','100000'),('down','103000','98000')])
        self.assertTrue(self.repo.read(int(items[0]['id']),('long',)))
        self.assertTrue(self.messages()[0]['is_read'])
        with http_read_scope():
            self.assertEqual(len(self.repo.rules()),2)
        r=self.client.patch('/alerts/rules/'+up['id'],json={'version':1,'enabled':False})
        self.assertEqual(r.status_code,200,r.text)
        self.snapshot(105000)
        self.repo.process(('long',))
        self.assertEqual(len(self.messages()),2)
        self.assertEqual(self.client.patch('/alerts/rules/'+up['id'],json={'version':1,'enabled':True}).status_code,409)
        self.assertTrue(self.client.delete('/alerts/rules/'+down['id']+'?version=1').is_success)

    def test_capture_snapshot_taken_before_rule_finalization(self):
        from concurrent.futures import ThreadPoolExecutor
        from time import monotonic, sleep
        from uuid import UUID
        survivor = self.add(rule_id=UUID(int=1))
        self.snapshot(99000)
        self.snapshot(101000)
        self.snapshot(91000)
        victim = self.add(rule_id=UUID(int=2))
        params = self.admin.info.get_parameters()
        params['password'] = os.environ['LIC_QUERY_TEST_PASSWORD']
        with psycopg.connect(**params, autocommit=True) as writer, ThreadPoolExecutor(2) as executor:
            writer.execute("SET lock_timeout='3s'")
            def wait_for(predicate):
                deadline = monotonic() + 2
                while not predicate():
                    if monotonic() >= deadline:
                        self.fail('concurrent transaction did not reach the expected lock boundary')
                    sleep(0.01)
            with self.admin.transaction():
                self.admin.execute("SELECT id FROM public.alert_rules WHERE id=%s FOR UPDATE", (survivor['id'],))
                pending = executor.submit(writer.execute, """INSERT INTO public.video_snapshots
                    (video_id,views,recorded_at,views_definition_epoch)
                    VALUES(%s,102000,clock_timestamp(),'test.views')""", (self.video,))
                wait_for(lambda:self.admin.execute("SELECT pg_backend_pid()=ANY(pg_blocking_pids(%s))",
                                                   (writer.info.backend_pid,)).fetchone()[0])
                self.assertEqual(self.client.delete('/alerts/rules/' + victim['id'] + '?version=1').status_code, 200)
                delivery = executor.submit(AlertsRepository(self.db).process, ('long',))
                # Delivery of the survivor is blocked by this transaction. The
                # empty victim must already be gone before delivery can commit.
                wait_for(lambda:not self.admin.execute("SELECT FROM public.alert_rules WHERE id=%s",
                                                       (victim['id'],)).fetchall())
            pending.result(timeout=4)
            delivery.result(timeout=4)
        self.repo.process(('long',))
        self.assertCountEqual([(r['previous_value'],r['current_value']) for r in self.messages()],
                              [('99000','101000'),('91000','102000')])
        self.assertEqual(self.admin.execute("SELECT id::text,gap_count FROM public.alert_rules").fetchall(),
                         [(survivor['id'],0)])

    def test_cleanup_progresses_with_another_rule_still_referenced(self):
        rules = sorted([self.add(), self.add()], key=lambda r:r['id'])
        ids = [r['id'] for r in rules]
        worker = AlertsRepository(self.db)
        with self.admin.transaction():
            self.admin.execute("SELECT id FROM public.alert_rules WHERE id=%s FOR KEY SHARE", (ids[0],)).fetchone()
            for rule_id in ids:
                self.assertEqual(self.client.delete('/alerts/rules/' + rule_id + '?version=1').status_code, 200)
            worker.process(('long',))
            worker.process(('long',))
            self.assertEqual(self.admin.execute("SELECT id::text FROM public.alert_rules").fetchall(), [(ids[0],)])
        self.assertGreater(worker.process(('long',)), 0)
        self.assertEqual(self.admin.execute("SELECT count(*) FROM public.alert_rules").fetchone(), (0,))

    def test_deleted_rule_cleanup_can_skip_a_locked_due_retry(self):
        removed = self.add()
        survivor = self.add()
        for v in (99000,101000,98000,102000):
            self.snapshot(v)
        self.admin.execute("UPDATE public.alert_observations SET retry_at=statement_timestamp() WHERE rule_id=%s", (survivor['id'],))
        self.assertEqual(self.client.delete('/alerts/rules/' + removed['id'] + '?version=1').status_code, 200)
        with self.admin.transaction():
            self.admin.execute("SELECT id FROM public.alert_observations WHERE rule_id=%s ORDER BY id LIMIT 1 FOR UPDATE",
                               (survivor['id'],)).fetchone()
            self.assertEqual(AlertsRepository(self.db).process(('long',)), 1)
            self.assertEqual([r['current_value'] for r in self.messages()], ['102000'])
        self.repo.process(('long',))
        self.assertCountEqual([r['current_value'] for r in self.messages()], ['101000','102000'])

    def test_rule_delete_during_capture_and_restart(self):
        removed = self.add()
        survivor = self.add()
        self.snapshot(99000)
        self.snapshot(101000)
        self.repo.process(('long',))
        obsolete = next(row for row in self.messages() if row['rule_id'] == removed['id'])
        self.snapshot(91000)
        with self.admin.transaction():
            self.snapshot(102000)
            endpoint = '/alerts/rules/' + removed['id']
            self.assertEqual(self.client.delete(endpoint + '?version=1').status_code, 200)
            self.assertEqual(self.client.delete(endpoint + '?version=1').status_code, 409)
            self.assertEqual(self.client.patch(endpoint, json={'version': 1, 'enabled': True}).status_code, 409)
            self.assertEqual(self.client.post('/alerts/rules', json={
                'id': removed['id'], 'rule': removed['definition']}).status_code, 409)
            self.assertEqual(self.client.post('/alerts/inbox/' + obsolete['id'] + '/read').status_code, 404)
            self.assertTrue(all(row['rule_id'] == survivor['id'] for row in self.messages()))
            self.repo.process(('long',))
            self.repo.process(('long',))
            self.assertTrue(self.admin.execute("SELECT EXISTS (SELECT FROM public.alert_rules WHERE id=%s)",
                                               (removed['id'],)).fetchone()[0])
        worker = AlertsRepository(self.db)
        for _ in range(5):
            worker.process(('long',))
        self.assertFalse(self.admin.execute("SELECT EXISTS (SELECT FROM public.alert_rules WHERE id=%s)",
                                            (removed['id'],)).fetchone()[0])
        for views in (90000,103000,89000,104000):
            self.snapshot(views)
        worker.process(('long',))
        received = list(reversed(self.messages()))
        self.assertEqual([(r['rule_id'],r['previous_value'],r['current_value']) for r in received], [
            (survivor['id'],str(before),str(after))
            for before,after in [(99000,101000),(91000,102000),(90000,103000),(89000,104000)]])
        self.assertEqual(self.admin.execute("SELECT gap_count FROM public.alert_rules").fetchall(), [(0,)])

    def test_catch_up_keeps_each_rise_and_replaying_evidence_is_idempotent(self):
        folder = fixture.FIXTURE['folders'][0]
        previous_folder = self.admin.execute("SELECT folder_id FROM public.channels WHERE channel_id=%s",
                                             (self.channel,)).fetchone()[0]
        self.addCleanup(self.admin.execute, "UPDATE public.channels SET folder_id=%s WHERE channel_id=%s",
                        (previous_folder, self.channel))
        self.admin.execute("UPDATE public.channels SET folder_id=%s WHERE channel_id=%s", (folder, self.channel))
        rule = self.add(scope={'kind': 'folder', 'id': folder})
        for views in (99000, 101000, 91000, 102000):
            self.snapshot(views)
        self.assertEqual(self.admin.execute("SELECT count(*) FROM public.video_snapshots WHERE video_id=%s",
                                           (self.video,)).fetchone(), (4,))
        self.assertEqual(self.admin.execute("SELECT count(*),count(enqueued_at) FROM public.alert_observations").fetchone(),
                         (2, 2))
        self.assertFalse(self.messages())
        # Keep an exact copy of the pending deliveries in this disposable session.
        self.admin.execute("CREATE TEMP TABLE replay_jobs AS SELECT * FROM public.alert_observations")
        self.addCleanup(self.admin.execute, "DROP TABLE replay_jobs")
        self.admin.execute("UPDATE public.channels SET folder_id=NULL WHERE channel_id=%s", (self.channel,))
        changed = dict(rule['definition'], name='Higher threshold', threshold='500000')
        self.assertEqual(self.client.patch('/alerts/rules/'+rule['id'],
                                          json={'version':1,'rule':changed}).status_code, 200)
        recovered = AlertsRepository(self.db)
        self.assertEqual(recovered.process(('long',)), 2)
        def inbox():
            response = self.client.get('/alerts/inbox')
            self.assertEqual(response.status_code, 200, response.text)
            return list(reversed(response.json()['items']))
        delivered = inbox()
        self.assertEqual([(r['previous_value'],r['current_value'],r['version']) for r in delivered],
                         [('99000','101000',1),('91000','102000',1)])
        self.assertLess(delivered[0]['observed_at'], delivered[1]['observed_at'])
        self.assertTrue(self.client.post('/alerts/inbox/'+delivered[0]['id']+'/read').is_success)
        self.admin.execute("INSERT INTO public.alert_observations OVERRIDING SYSTEM VALUE SELECT * FROM replay_jobs")
        restarted = AlertsRepository(self.db)
        self.assertEqual(restarted.process(('long',)), 2)
        self.assertEqual(restarted.process(('long',)), 0)
        self.assertEqual([(r['id'],r['is_read']) for r in inbox()],
                         [(delivered[0]['id'], True),(delivered[1]['id'], False)])

    def test_queue_boundaries_for_both_directions_and_each_metric(self):
        for event in ('video.views', 'video.likes', 'video.comments'):
            for direction in ('up', 'down'):
                self.add(event, direction=direction)
        for value in (99000, 100000, 100000, 101000, 100000, 91000, 102000):
            self.snapshot(value, value, value)
        queued = self.admin.execute("""SELECT target.definition->>'direction',count(*)
            FROM public.alert_observations o CROSS JOIN LATERAL (
                SELECT o.definition WHERE o.members IS NULL UNION ALL
                SELECT value->'rule' FROM jsonb_each(o.members)
            ) target(definition) GROUP BY 1 ORDER BY 1""").fetchall()
        self.assertEqual(queued, [('down',15), ('up',15)])
        self.repo.process(('long',))
        self.assertCountEqual([(r['event'],r['direction']) for r in self.messages()],
                              [(event, direction) for event in ('video.views','video.likes','video.comments')
                               for direction in ('up','down','up')])

    def test_upgrade_preserves_pending_evidence_and_leaves_its_enqueue_time_unknown(self):
        self.snapshot(99000)
        self.add()
        self.snapshot(101000)
        step, = (ROOT/'schema/steps').glob('*-alert-queue-candidates.sql')
        with self.admin.transaction():
            self.admin.execute("ALTER TABLE public.alert_observations DROP COLUMN enqueued_at")
            before = self.admin.execute("SELECT to_jsonb(o) FROM public.alert_observations o").fetchone()[0]
            original = self.admin.execute("SELECT pg_get_functiondef('public.capture_alert_observations()'::regprocedure)").fetchone()[0]
            self.addCleanup(self.admin.execute, original)
            for trigger, in self.admin.execute("""SELECT pg_get_triggerdef(oid) FROM pg_trigger
                WHERE tgname IN ('alert_video_observations','alert_availability_observations')"""):
                self.addCleanup(self.admin.execute,trigger.replace('CREATE TRIGGER','CREATE OR REPLACE TRIGGER',1))
            self.admin.execute(step.read_text(), prepare=False)
            row = self.admin.execute("""SELECT to_jsonb(o)-'enqueued_at', enqueued_at FROM public.alert_observations o
                WHERE id=%s""", (before['id'],)).fetchone()
            self.assertEqual(row, (before,None))
            self.snapshot(91000)
            self.snapshot(102000)
        self.assertEqual(self.admin.execute("SELECT count(*),count(enqueued_at) FROM public.alert_observations").fetchone(),
                         (2,1))
        self.assertEqual(self.repo.process(('long',)), 2)
        self.assertCountEqual([r['current_value'] for r in self.messages()], ['101000','102000'])

    def test_alert_capture_matches_the_installed_readiness_contract(self):
        from db.readiness.intrinsic_capabilities import assert_final_schema_ready
        assert_final_schema_ready(self.db)

    def test_paused_worker_drains_multiple_batches_without_collapsing_crossings(self):
        self.add()
        self.admin.execute("UPDATE public.alert_rules SET started_at=clock_timestamp()-interval '2 seconds'")
        self.admin.execute("UPDATE public.videos SET published_at=clock_timestamp()-interval '1 day' WHERE video_id=%s",
                           (self.video,))
        sql = (ROOT/'tools/alert_queue_sample.sql').read_text()
        def sample():
            cur = self.admin.execute(sql, prepare=False)
            while cur.description is None:
                self.assertTrue(cur.nextset())
            fields = [column.name for column in cur.description]
            return dict(zip(fields, cur.fetchone()))
        self.admin.execute("""INSERT INTO public.video_snapshots(video_id,views,recorded_at,views_definition_epoch)
            SELECT %s,(ARRAY[99000,101000,91000,102000])[(i-1)%%4+1],
                   statement_timestamp()-(481-i)*interval '1 millisecond','test.views'
            FROM generate_series(1,480) AS i ORDER BY i""", (self.video,))
        queued = self.admin.execute("SELECT count(*) FROM public.alert_observations").fetchone()[0]
        self.assertEqual(queued, 240)
        before = sample()
        self.assertGreaterEqual(before['oldest_known_wait_seconds'], 0)
        self.assertFalse(before['contains_jobs_with_unknown_wait'])
        self.admin.execute("""UPDATE public.alert_observations SET enqueued_at=NULL
            WHERE id=(SELECT min(id) FROM public.alert_observations)""")
        self.assertTrue(sample()['contains_jobs_with_unknown_wait'])
        repo = AlertsRepository(self.db)
        batches = [repo.process(('long',)) for _ in range(4)]
        self.assertEqual(batches, [100,100,40,0])
        result = self.admin.execute("""SELECT count(*),count(DISTINCT observed_at),
            count(*) FILTER (WHERE (previous_value,current_value) IN ((99000,101000),(91000,102000)))
            FROM public.alert_inbox""").fetchone()
        self.assertEqual(result, (240,240,240))
        after = sample()
        self.assertIsNone(after['oldest_known_wait_seconds'])
        self.assertFalse(after['contains_jobs_with_unknown_wait'])

    def test_shared_counter_keeps_each_recipient_and_repeat_crossing(self):
        self.snapshot(99)
        rules = [self.add(name=f'Owner {i}',threshold='100') for i in range(8)]
        for value in (101, 91, 102):
            self.snapshot(value)
        self.assertEqual(self.admin.execute("""SELECT count(*),count(members),sum(jsonb_array_length(
            jsonb_path_query_array(members,'$.keyvalue().key'))) FROM public.alert_observations""").fetchone(),
                         (2,2,16))
        self.repo.delete(rules[0]['id'],1)
        self.repo.change(rules[1]['id'],1,None,False)
        self.repo.change(rules[2]['id'],1,AlertRule.model_validate(dict(rules[2]['definition'],name='New name')),None)
        self.admin.execute("""UPDATE public.alert_observations SET members=jsonb_set(members,%s,'{}')
            WHERE members IS NOT NULL""", ([rules[3]['id'],'rule'],))
        restarted = AlertsRepository(self.db)
        for _ in range(5):
            restarted.process(('long',))
        items = self.admin.execute("""SELECT rule_name,count(*),min(version),count(DISTINCT source_key)
            FROM readable.alert_inbox GROUP BY rule_name ORDER BY rule_name""").fetchall()
        self.assertEqual(items,[(f'Owner {i}',2,1,2) for i in (1,2,4,5,6,7)])
        self.assertEqual(self.admin.execute("SELECT gap_count FROM public.alert_rules WHERE id=%s",
                                            (rules[3]['id'],)).fetchone(),(2,))

    def test_one_unreadable_rule_clock_keeps_healthy_shared_recipients(self):
        self.snapshot(99)
        rules=[self.add(name=f'Watch {i}',threshold='100') for i in range(5)]
        self.admin.execute("UPDATE public.alert_rules SET started_at='-infinity' WHERE id=%s",(rules[0]['id'],))
        self.snapshot(101)
        self.repo.process(('long',))
        self.assertEqual(len(self.messages()),4)
        self.assertEqual(self.admin.execute("SELECT gap_count FROM public.alert_rules WHERE id=%s",
                                          (rules[0]['id'],)).fetchone(),(1,))

    def test_shared_pair_uses_late_measurement_and_individual_boundary(self):
        self.snapshot(101)
        rules = [self.add(name=f'Watch {i}',threshold='100') for i in range(6)]
        earlier = self.admin.execute("SELECT clock_timestamp()").fetchone()[0]
        self.snapshot(105)
        self.admin.execute("""INSERT INTO public.video_snapshots(video_id,views,recorded_at,views_definition_epoch)
            VALUES(%s,91,%s,'test.views')""",(self.video,earlier))
        self.admin.execute("""UPDATE public.alert_observations SET members=jsonb_set(members,%s,to_jsonb(observed_at))
            WHERE members IS NOT NULL""",([rules[0]['id'],'since'],))
        self.repo.process(('long',))
        self.assertCountEqual([str(i['rule_id']) for i in self.messages()],[r['id'] for r in rules[1:]])

    def test_shared_timeout_isolates_only_the_slow_recipient(self):
        self.snapshot(99)
        rules=[self.add(name=f'Fan {i}',threshold='100') for i in range(6)]
        self.snapshot(101)
        self.admin.execute("""CREATE FUNCTION public.alert_shared_slow() RETURNS trigger LANGUAGE plpgsql
            AS $$ BEGIN IF NEW.rule_name='Fan 0' THEN PERFORM pg_sleep(6); END IF; RETURN NEW; END $$""")
        self.addCleanup(self.admin.execute,"DROP FUNCTION public.alert_shared_slow() CASCADE")
        self.admin.execute("""CREATE TRIGGER alert_shared_slow BEFORE INSERT ON public.alert_inbox
            FOR EACH ROW EXECUTE FUNCTION public.alert_shared_slow()""")
        self.assertGreater(self.repo.process(('long',)),0)
        self.assertEqual(self.admin.execute("SELECT count(*) FROM public.alert_observations").fetchone()[0],6)
        for _ in rules:
            self.repo.process(('long',))
        self.assertEqual(len(self.messages()),5)
        self.admin.execute("DROP TRIGGER alert_shared_slow ON public.alert_inbox")
        self.admin.execute("UPDATE public.alert_observations SET retry_at=statement_timestamp()")
        self.assertEqual(AlertsRepository(self.db).process(('long',)),1)
        self.assertEqual(len(self.messages()),6)

    def test_unknown_engagement_and_redefined_views(self):
        self.add('video.likes')
        self.add()
        self.snapshot(99000,99000,epoch='old')
        self.snapshot(101000,None,epoch='new')
        self.snapshot(103000,101000,epoch='new')
        self.repo.process(('long',))
        self.assertEqual([i['event'] for i in self.messages()],['video.likes'])

    def test_preview_and_owner(self):
        r=self.add(threshold='0')
        self.assertEqual(self.client.get('/alerts/rules',headers={'Authorization':'guest'}).status_code,401)
        result=self.client.post('/alerts/preview',json=r['definition'])
        self.assertEqual(result.status_code,200,result.text)
        self.assertTrue(result.json()['rows'])
        higher = self.client.post('/alerts/preview',json={**r['definition'],'threshold':str(2**63-1)})
        self.assertEqual(higher.status_code,200,higher.text)
        self.assertEqual(higher.json()['rows'],[])
        self.assertFalse(self.messages())

    def test_publication_and_availability(self):
        self.add('video.published')
        self.add('video.unavailable')
        self.add('video.restored')
        self.snapshot(120000)
        self.repo.process(('long',))
        self.assertFalse(self.messages())
        self.admin.execute("UPDATE public.videos SET published_at=clock_timestamp(),metadata_checked_at=clock_timestamp() WHERE video_id=%s",(self.video,))
        self.snapshot(120001)
        self.snapshot(120002)
        for event,status in (('unavailable_detected','unknown_unavailable'),('unavailable_status_changed','private'),('restored','public')):
            self.admin.execute("""INSERT INTO public.video_availability_events(video_id,channel_id,event_type,to_status,observed_at)
                VALUES(%s,%s,%s,%s,clock_timestamp())""",(self.video,self.channel,event,status))
        self.repo.process(('long',))
        self.assertCountEqual([i['event'] for i in self.messages()],['video.published','video.unavailable','video.restored'])

    def test_capture_error_preserves_the_source_and_reports_a_gap(self):
        self.add()
        self.admin.execute("ALTER TABLE public.alert_observations ADD CONSTRAINT fixture_closed_queue CHECK (false)")
        try:
            row=self.snapshot(100001)
            self.assertEqual(self.admin.execute("SELECT views FROM public.video_snapshots WHERE id=%s",(row,)).fetchone(),(100001,))
            self.assertEqual(self.repo.rules()[0]['gap_count'],1)
        finally:
            self.admin.execute("ALTER TABLE public.alert_observations DROP CONSTRAINT fixture_closed_queue")

    def test_stopped_worker_expiry_uses_normal_retention(self):
        self.snapshot(99000)
        self.add()
        self.snapshot(101000)
        self.repo.process(('long',))
        self.snapshot(104000)
        self.admin.execute("UPDATE public.alert_inbox SET expires_at=statement_timestamp()-interval '1 second'")
        self.admin.execute("UPDATE public.alert_observations SET evidence_expires_at=statement_timestamp()-interval '1 second'")
        for target in ('alert_observations','alert_inbox'):
            receipt=self.db.cleanup_hot_evidence(target=target,limit=10)
            self.assertEqual(receipt['deleted'],1,receipt)
        self.assertEqual(self.repo.rules()[0]['gap_count'],1)
        self.assertFalse(self.messages())

    def test_retention_counts_expired_shared_observations_per_live_recipient(self):
        self.snapshot(99)
        rules=[self.add(name=f'Retention {i}',threshold='100') for i in range(6)]
        for value in (101,91,102):
            self.snapshot(value)
        self.repo.change(rules[0]['id'],1,None,False)
        self.repo.delete(rules[1]['id'],1)
        self.admin.execute("UPDATE public.alert_observations SET evidence_expires_at=clock_timestamp()-interval '1 second'")
        self.assertEqual(self.db.cleanup_hot_evidence(target='alert_observations',limit=10)['deleted'],2)
        actual=dict(self.admin.execute("SELECT id::text,gap_count FROM public.alert_rules").fetchall())
        self.assertEqual(actual,{r['id']:0 if i==1 else 2 for i,r in enumerate(rules)})

    def test_alert_expiry_cursor_crosses_tied_and_future_clocks(self):
        self.snapshot(99)
        self.add(threshold='100')
        for value in (101,91,102,81,103,71,104):
            self.snapshot(value)
        self.repo.process(('long',))
        self.snapshot(105)
        self.snapshot(106)
        self.snapshot(107)
        self.snapshot(108)
        # Two expired rows share a clock; an invalid future row is on the
        # other side of a valid row in the index.
        for table,clock in [('alert_observations','evidence_expires_at'),('alert_inbox','expires_at')]:
            ids=[row[0] for row in self.admin.execute(f'SELECT id FROM public.{table} ORDER BY id')]
            self.assertEqual(len(ids),4)
            self.admin.execute(f"""UPDATE public.{table} SET {clock}=CASE
                WHEN id=ANY(%s) THEN now()-interval '1 day'
                WHEN id=%s THEN 'infinity'::timestamptz ELSE now()+interval '1 day' END""",(ids[:2],ids[-1]))
            cursor=None
            for expected in (1,1,1,0):
                page=self.db.cleanup_hot_evidence(target=table,after_id=cursor,limit=1)
                self.assertEqual(page['deleted'],expected,page)
                cursor=page['next_cursor']
                self.assertEqual(page['complete'],expected==0)
            self.assertEqual(self.admin.execute(f'SELECT id FROM public.{table}').fetchall(),[(ids[2],)])
        self.assertEqual(self.repo.rules()[0]['gap_count'],3)

    def test_channel_and_video_require_the_same_control_owner_as_capture(self):
        rules=[self.add(),self.add(scope={'kind':'video','id':self.video})]
        owner=self.admin.execute("SELECT user_work_id FROM public.channels WHERE channel_id=%s",(self.channel,)).fetchone()[0]
        self.admin.execute("UPDATE public.channel_collection_state SET user_work_id=%s WHERE channel_id=%s",(uuid4(),self.channel))
        self.addCleanup(self.admin.execute,"UPDATE public.channel_collection_state SET user_work_id=%s WHERE channel_id=%s",(owner,self.channel))
        self.assertFalse(any(r['source_available'] for r in self.repo.rules()))
        self.snapshot(101000)
        self.assertEqual(self.admin.execute("SELECT count(*) FROM public.alert_observations").fetchone(),(0,))
        for rule in rules:
            response=self.client.post('/alerts/rules',json={'id':str(uuid4()),'rule':rule['definition']})
            self.assertEqual(response.status_code,409,response.text)

    def test_trash_return_fences_each_scope_and_late_availability(self):
        from db.channel.channel_control import set_channel_trash_state
        folder=self.admin.execute("SELECT folder_id::text FROM public.channels WHERE channel_id=%s",(self.channel,)).fetchone()[0]
        label='custom:'+str(uuid4())
        self.admin.execute("INSERT INTO public.channel_labels(label_key,name,label_type) VALUES(%s,'Trash test','custom')",(label,))
        self.addCleanup(self.admin.execute,"DELETE FROM public.channel_labels WHERE label_key=%s",(label,))
        self.admin.execute("INSERT INTO public.channel_label_assignments(channel_id,label_key) VALUES(%s,%s)",(self.channel,label))
        scopes=[{'kind':kind,'id':identity} for kind,identity in (
            ('channel',self.channel),('video',self.video),('folder',folder),('label',label))]
        ids=[self.add(scope=s)['id'] for s in scopes]
        self.add(event='video.unavailable')
        self.add(event='video.restored')
        self.snapshot(99000)
        self.snapshot(100001)
        self.repo.process(('long',))
        self.assertCountEqual([str(item['rule_id']) for item in self.messages()],ids)
        trash=set_channel_trash_state(self.db,channel_id=self.channel)
        self.assertEqual(trash['outcome'],'removed')
        def restore():
            return set_channel_trash_state(self.db,user_work_id=trash['id'],expected_hidden_at=trash['removed_at'],restore=True)
        self.addCleanup(restore)
        hidden_at=self.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        # The trash operation updates channel collection state.
        self.assertFalse(self.admin.execute("SELECT is_hidden FROM public.channels WHERE channel_id=%s",(self.channel,)).fetchone()[0])
        states={r['id']:r['source_available'] for r in self.repo.rules()}
        self.assertEqual([states[i] for i in ids],[False,False,True,True])
        self.assertFalse(self.messages())
        self.snapshot(99000)
        self.snapshot(100002)
        self.assertEqual(self.admin.execute("SELECT count(*) FROM public.alert_observations").fetchone(),(0,))
        self.assertEqual(restore()['outcome'],'restored')
        self.assertCountEqual([str(item['rule_id']) for item in self.messages()],ids)
        self.assertTrue(all(r['source_available'] for r in self.repo.rules()))
        self.snapshot(100003)
        for kind,status in [('unavailable_detected','unknown_unavailable'),('restored','public')]:
            self.admin.execute('''INSERT INTO public.video_availability_events(video_id,channel_id,event_type,to_status,observed_at)
                VALUES(%s,%s,%s,%s,%s)''',(self.video,self.channel,kind,status,hidden_at))
        self.repo.process(('long',))
        self.assertEqual(len(self.messages()),4)
        self.snapshot(99000)
        self.snapshot(100004)
        for kind,status in [('unavailable_detected','unknown_unavailable'),('restored','public')]:
            self.admin.execute('''INSERT INTO public.video_availability_events(video_id,channel_id,event_type,to_status,observed_at)
                VALUES(%s,%s,%s,%s,clock_timestamp())''',(self.video,self.channel,kind,status))
        self.repo.process(('long',))
        self.assertCountEqual([i['event'] for i in self.messages()],['video.views']*8+['video.unavailable','video.restored'])

    def test_upgrade_binds_live_null_folders_and_preserves_orphan_and_tombstone(self):
        folder=str(uuid4())
        step,=(ROOT/'schema/steps').glob('0061-*.sql')
        with self.admin.transaction(force_rollback=True):
            # Start with rules whose sources have no stored identity.
            self.admin.execute('DROP VIEW readable.alert_rules')
            self.admin.execute('ALTER TABLE public.alert_rules DROP COLUMN restored_scope_since')
            self.admin.execute('DROP TRIGGER alert_visibility_clock ON public.channel_collection_state')
            self.admin.execute('DROP FUNCTION public.alert_visibility_clock()')
            self.admin.execute('ALTER TABLE public.channel_collection_state DROP COLUMN alert_visible_since')
            self.admin.execute('ALTER TABLE public.folders DROP COLUMN alert_identity')
            self.admin.execute('ALTER TABLE public.channel_labels DROP COLUMN alert_identity')
            self.admin.execute("INSERT INTO public.folders(id,name,created_at) VALUES(%s,'Migration',NULL)",(folder,))
            rule_ids=[uuid4() for _ in range(4)]
            for rule_id,source,created,deleted in zip(rule_ids,[folder,str(uuid4()),folder,folder],
                    [None,None,'2001-01-01',None],[False,False,False,True]):
                self.admin.execute('''INSERT INTO public.alert_rules(id,definition,scope_created_at,enabled,deleted_at)
                    VALUES(%s,jsonb_build_object('scope',jsonb_build_object('kind','folder','id',%s::text)),
                           %s,NOT %s,CASE WHEN %s THEN clock_timestamp() END)''',(rule_id,source,created,deleted,deleted))
            self.admin.execute(step.read_text(),prepare=False)
            self.assertEqual(dict(self.admin.execute("SELECT id,source_available FROM readable.alert_rules")),
                             {rule_ids[0]:True,rule_ids[1]:False,rule_ids[2]:False})
            self.assertIsNone(self.admin.execute("SELECT scope_owner FROM public.alert_rules WHERE id=%s",(rule_ids[3],)).fetchone()[0])

    def test_null_folder_lifetime_and_gap_scope(self):
        folder=str(uuid4())
        def make_folder():
            self.admin.execute("INSERT INTO public.folders(id,name,created_at) VALUES(%s,'No timestamp',NULL)",(folder,))
        def assign(value):
            self.admin.execute("UPDATE public.channels SET folder_id=%s WHERE channel_id=%s",(value,self.channel))
        original=self.admin.execute("SELECT folder_id FROM public.channels WHERE channel_id=%s",(self.channel,)).fetchone()[0]
        make_folder()
        self.addCleanup(self.admin.execute,"DELETE FROM public.folders WHERE id=%s",(folder,))
        self.addCleanup(assign,original)
        rule=self.add(scope={'kind':'folder','id':folder})['id']
        self.assertTrue(self.repo.rules()[0]['source_available'])
        late_at=self.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        assign(folder)
        self.snapshot(99000)
        self.snapshot(101000)
        self.repo.process(('long',))
        self.assertEqual([str(i['rule_id']) for i in self.messages()],[rule])
        self.admin.execute("DELETE FROM public.folders WHERE id=%s",(folder,))
        make_folder()
        assign(folder)
        replacement=self.add(scope={'kind':'folder','id':folder})['id']
        direct=self.add()['id']
        self.admin.execute("UPDATE public.alert_rules SET started_at=%s::timestamptz-interval '1 second' WHERE id=ANY(%s::uuid[])",
                           (late_at,[direct,replacement]))
        wrong_duration=self.add(min_duration=9007199254740991)['id']
        wrong_metric=self.add(event='video.likes')['id']
        wrong_direction=self.add(direction='down')['id']
        paused=self.add()['id']
        self.repo.change(paused,1,None,False)
        deleted=self.add()['id']
        self.repo.delete(deleted,1)
        self.admin.execute("ALTER TABLE public.alert_observations ADD CONSTRAINT fixture_closed_scope_queue CHECK(false)")
        try:
            self.admin.execute('''INSERT INTO public.video_snapshots(video_id,views,recorded_at,views_definition_epoch)
                VALUES(%s,101000,%s,'test.views'),(%s,102000,clock_timestamp(),'test.views'),
                (%s,103000,clock_timestamp(),'test.views')''',(self.video,late_at,self.video,self.video))
            states={r['id']:r['source_available'] for r in self.repo.rules()}
            self.assertEqual([states[rule],states[replacement]],[False,True])
            gaps=dict(self.admin.execute("SELECT id::text,gap_count FROM public.alert_rules"))
            self.assertEqual(gaps,{rule:0,replacement:2,direct:3,wrong_duration:0,wrong_metric:0,wrong_direction:0,paused:0,deleted:0})
        finally:
            self.admin.execute("ALTER TABLE public.alert_observations DROP CONSTRAINT fixture_closed_scope_queue")
        self.snapshot(97000)
        self.snapshot(104000)
        self.repo.process(('long',))
        self.assertCountEqual([str(i['rule_id']) for i in self.messages() if i['direction']=='up'],[rule,replacement,direct])

    def test_label_assignment_after_transaction_start_begins_a_new_baseline(self):
        key='custom:'+str(uuid4())
        self.admin.execute("INSERT INTO public.channel_labels(label_key,name,label_type) VALUES(%s,'Scope clock','custom')",(key,))
        self.addCleanup(self.admin.execute,"DELETE FROM public.channel_labels WHERE label_key=%s",(key,))
        rule=self.add(scope={'kind':'label','id':key})['id']
        with psycopg.connect(self.admin.info.dsn,password=os.environ['LIC_QUERY_TEST_PASSWORD']) as assigner:
            assigner.execute('SELECT now()')
            self.snapshot(99000)
            assigner.execute("INSERT INTO public.channel_label_assignments(channel_id,label_key) VALUES(%s,%s)",(self.channel,key))
        self.snapshot(100001)
        self.repo.process(('long',))
        self.assertFalse(self.messages())
        self.snapshot(99000)
        self.snapshot(100002)
        self.repo.process(('long',))
        self.assertEqual([str(i['rule_id']) for i in self.messages()],[rule])

    def test_failed_replay_counts_missing_recipients_but_recognizes_queued_and_delivered_work(self):
        ids=[self.add()['id'] for _ in range(2)]
        self.snapshot(99000)
        scalar=self.snapshot(101000)
        ids.extend(self.add()['id'] for _ in range(3))
        self.snapshot(99000)
        shared=self.snapshot(102000)
        self.admin.execute("DELETE FROM public.alert_observations WHERE rule_id=%s AND source_key=%s",(ids[0],str(scalar)))
        self.admin.execute("UPDATE public.alert_observations SET members=members-%s::text WHERE source_key=%s",(ids[-1],str(shared)))
        self.admin.execute("CREATE TEMP TABLE replay_coverage AS SELECT * FROM public.video_snapshots WHERE id IN (%s,%s)",(scalar,shared))
        self.addCleanup(self.admin.execute,"DROP TABLE replay_coverage")
        def replay():
            self.admin.execute("DELETE FROM public.video_snapshots WHERE id IN (%s,%s)",(scalar,shared))
            self.admin.execute("INSERT INTO public.video_snapshots SELECT * FROM replay_coverage")
        self.admin.execute("ALTER TABLE public.alert_observations ADD CONSTRAINT fixture_failed_replay CHECK(false) NOT VALID")
        try:
            for attempts in range(1,4):
                replay()
                gaps=dict(self.admin.execute("SELECT id::text,gap_count FROM public.alert_rules"))
                self.assertEqual([gaps[i] for i in ids],[attempts,0,0,0,attempts])
                if attempts==2:
                    self.repo.process(('long',))
                    self.assertEqual(len(self.messages()),5)
        finally:
            self.admin.execute("ALTER TABLE public.alert_observations DROP CONSTRAINT fixture_failed_replay")
        replay()
        self.repo.process(('long',))
        self.assertEqual(len(self.messages()),7)

    def test_failed_fact_projection_isolated_from_other_facts_in_same_statement(self):
        rule=self.add(event='channel.subscribers')['id']
        original=self.admin.execute("SELECT pg_get_triggerdef(oid) FROM pg_trigger WHERE tgname='alert_channel_observations'").fetchone()[0]
        ddl=original.replace('CREATE TRIGGER','CREATE OR REPLACE TRIGGER',1)
        broken=ddl.replace('s.subscribers AS measured_value',
                           's.subscribers / CASE WHEN s.subscribers=101001 THEN 0 ELSE 1 END AS measured_value')
        self.assertNotEqual(broken,ddl)
        try:
            self.admin.execute(broken)
            self.admin.execute('''INSERT INTO public.channel_snapshots(channel_id,subscribers,recorded_at,source)
                SELECT %s,value,clock_timestamp()+offset_us*interval '1 microsecond','youtube_channel_metadata_refresh'
                FROM (VALUES (99000,-3),(101001,-2),(102000,-1)) samples(value,offset_us)''',(self.channel,))
            self.assertEqual(self.admin.execute("SELECT count(*) FROM public.channel_snapshots WHERE channel_id=%s AND subscribers=101001",
                                               (self.channel,)).fetchone(),(1,))
            self.assertEqual(self.admin.execute("SELECT rule_id::text FROM public.alert_observations").fetchall(),[(rule,)])
            self.assertEqual(self.repo.rules()[0]['gap_count'],0)
        finally:
            self.admin.execute(ddl)

    def test_folder_membership_and_expiry(self):
        folder = fixture.FIXTURE['folders'][0]
        self.admin.execute("UPDATE public.channels SET folder_id=NULL WHERE channel_id=%s",(self.channel,))
        rule=self.add(scope={'kind':'folder','id':folder})
        self.snapshot(99000)
        self.admin.execute("UPDATE public.channels SET folder_id=%s WHERE channel_id=%s",(folder,self.channel))
        self.snapshot(102000)
        self.repo.process(('long',))
        self.assertFalse(self.messages())
        self.snapshot(99000)
        self.snapshot(101000)
        self.repo.process(('long',))
        self.assertEqual(len(self.messages()),1)
        self.admin.execute("UPDATE public.alert_inbox SET expires_at=clock_timestamp()-interval '1 second'")
        self.assertFalse(self.messages())
        self.snapshot(104000)
        self.admin.execute("UPDATE public.alert_observations SET evidence_expires_at=clock_timestamp()-interval '1 second'")
        self.repo.process(('long',))
        self.assertEqual(self.repo.rules()[0]['gap_count'],1)
        self.assertEqual(self.admin.execute("SELECT count(*) FROM public.alert_inbox").fetchone()[0],0)

    def test_format_single_video_and_older_transaction_committing_last(self):
        other='s0000000001'
        self.add(scope={'kind':'video','id':self.video})
        self.add(scope={'kind':'video','id':other},video_type='short')
        self.snapshot(99000,video=self.video)
        self.snapshot(99000,video=other)
        self.repo.process(('long','short'))
        with psycopg.connect(self.admin.info.dsn,password=os.environ['LIC_QUERY_TEST_PASSWORD']) as earlier:
            earlier.execute("""INSERT INTO public.video_snapshots(video_id,views,recorded_at,views_definition_epoch)
              VALUES(%s,100000,clock_timestamp(),'test.views')""",(other,))
            self.snapshot(103000,video=self.video)
            self.repo.process(('long','short'))
            self.assertEqual([r['subject'] for r in self.messages()],[self.video])
        self.repo.process(('long','short'))
        self.assertCountEqual([r['subject'] for r in self.messages()],[self.video,other])

    def test_initial_hydration_is_silent_even_for_a_recent_publication(self):
        self.add('video.published')
        self.admin.execute("UPDATE public.videos SET published_at=clock_timestamp() WHERE video_id=%s",(self.video,))
        self.admin.execute("""UPDATE public.video_inventory_memberships m SET first_seen_scan_id=s.scan_id
            FROM public.channel_video_inventory_scans s WHERE m.video_id=%s AND m.channel_id=s.channel_id
            AND m.video_type=s.video_type AND s.scan_kind='initial_inventory'""",(self.video,))
        try:
            self.snapshot(1)
            self.repo.process(('long',))
            self.assertFalse(self.messages())
        finally:
            self.admin.execute("UPDATE public.video_inventory_memberships SET first_seen_scan_id=NULL WHERE video_id=%s",(self.video,))

    def test_recreated_label_does_not_recapture_a_rule(self):
        key='custom:'+str(uuid4())
        stamp=self.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        def label():
            self.admin.execute("INSERT INTO public.channel_labels(label_key,name,label_type,created_at) VALUES(%s,'Sample','custom',%s)",(key,stamp))
            self.admin.execute("INSERT INTO public.channel_label_assignments(channel_id,label_key) VALUES(%s,%s)",(self.channel,key))
        label()
        rule=self.add(scope={'kind':'label','id':key})
        self.admin.execute("DELETE FROM public.channel_labels WHERE label_key=%s",(key,))
        label()
        self.snapshot(99000)
        self.snapshot(101000)
        self.assertEqual(self.repo.process(('long',)),0)
        self.assertFalse(self.repo.rules()[0]['source_available'])
        self.assertTrue(self.client.patch('/alerts/rules/'+rule['id'],json={'version':1,'enabled':False}).is_success)
        self.assertEqual(self.client.patch('/alerts/rules/'+rule['id'],json={'version':2,'enabled':True}).status_code,409)

    def test_transaction_rolls_back_after_inbox_insert(self):
        self.snapshot(99000)
        self.add()
        self.snapshot(100001)
        from db.base.postgres import Transaction
        from unittest.mock import patch
        execute=Transaction.execute
        def interrupt(tx,statement,parameters=()):
            if statement.startswith('DELETE FROM public.alert_observations'):
                raise RuntimeError('simulated interruption')
            return execute(tx,statement,parameters)
        with patch.object(Transaction,'execute',interrupt),self.assertRaises(RuntimeError):
            self.repo.process(('long',))
        self.assertFalse(self.messages())
        self.repo.process(('long',))
        self.assertEqual(len(self.messages()),1)

    def test_collected_batches_deliver_each_crossing_for_each_video(self):
        from db.videos.video_counter_completion import complete_video_counter_collection
        from research_home_pg import bulk

        video_ids = ['z0000000001', 'z0000000002']
        now = self.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        for table in ('video_inventory', 'videos', 'video_snapshots'):
            self.addCleanup(self.admin.execute, 'DELETE FROM public.' + table + ' WHERE video_id = ANY(%s)', (video_ids,))
        bulk(self.admin, self.channel, 'z', now, len(video_ids))
        self.admin.execute("""INSERT INTO public.video_collection_state(video_id, channel_id, video_type)
            SELECT unnest(%s::text[]), %s, 'long' ON CONFLICT DO NOTHING""", (video_ids, self.channel))
        self.add()
        for views in (99000, 101000, 91000, 102000):
            token = str(uuid4())
            self.admin.execute("""UPDATE public.video_collection_state SET lease_token=%s,
                lease_owner='synthetic-counter-batch', lease_started_at=now(), lease_expires_at=now()+interval '5 minutes',
                lease_channel_epoch=(SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s)
                WHERE video_id = ANY(%s)""", (token, self.channel, video_ids))
            at = self.admin.execute('SELECT clock_timestamp()').fetchone()[0].isoformat()
            rows = [dict(video_id=vid, lease_token=token, collection_attempt_id=str(uuid4()),
                         views=views, like_count=1, comment_count=0, recorded_at=at) for vid in video_ids]
            self.assertEqual(complete_video_counter_collection(self.db, rows), len(video_ids))
        self.repo.process(('long',))
        self.assertCountEqual([(row['subject'], row['previous_value'], row['current_value']) for row in self.messages()],
                              [(vid, previous, current) for vid in video_ids
                               for previous, current in [('99000', '101000'), ('91000', '102000')]])

    def test_damaged_observations_do_not_hide_a_healthy_crossing(self):
        self.snapshot(99000)
        ids = [self.add(name=name)['id'] for name in ('bad clock', 'bad rule', 'bad format', 'healthy')]
        self.snapshot(100001)
        self.admin.execute("""UPDATE public.alert_observations SET published_at='-infinity'
                              WHERE rule_id=%s""", (ids[0],))
        self.admin.execute("""UPDATE public.alert_observations SET definition='{}'
                              WHERE rule_id=%s""", (ids[1],))
        self.admin.execute("UPDATE public.alert_observations SET video_type='invalid' WHERE rule_id=%s", (ids[2],))
        for _ in range(5):
            self.repo.process(('long',))
        self.assertEqual([str(item['rule_id']) for item in self.messages()], [ids[3]])
        gaps = {r['id']: r['gap_count'] for r in self.client.get('/alerts/rules').json()['items']}
        self.assertEqual([gaps[i] for i in ids], [1, 1, 1, 0])
        self.assertEqual(AlertsRepository(self.db).process(('long',)), 0)

    def test_unreadable_source_key_duration_and_rule_number_are_local_gaps(self):
        self.snapshot(99000)
        ids = [self.add(name='key')['id'], self.add(name='duration', min_duration=1)['id'],
               self.add(name='number')['id'], self.add(name='healthy')['id']]
        self.snapshot(100001)
        self.admin.execute("UPDATE public.alert_observations SET source_key='malformed' WHERE rule_id=%s", (ids[0],))
        self.admin.execute("UPDATE public.alert_observations SET duration=repeat('1',5000) WHERE rule_id=%s", (ids[1],))
        self.admin.execute("""UPDATE public.alert_observations SET definition=jsonb_build_object(
            'threshold',repeat('9',5000)::numeric) WHERE rule_id=%s""", (ids[2],))
        self.repo.process(('long',))
        gaps = {r['id']: r['gap_count'] for r in self.client.get('/alerts/rules').json()['items']}
        self.assertEqual([gaps[i] for i in ids], [1, 1, 1, 0])
        self.assertEqual([str(r['rule_id']) for r in self.messages()], [ids[3]])

    def test_timeout_isolated_and_retried_after_restart(self):
        self.snapshot(99000)
        for name in ('slow', 'healthy'):
            self.add(name=name)
        self.snapshot(100001)
        # Force a real PostgreSQL statement timeout for one rule on the disposable DB.
        self.admin.execute("""CREATE FUNCTION public.alert_fixture_cancel() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN IF NEW.rule_name='slow' THEN
            PERFORM pg_sleep(8); END IF; RETURN NEW; END $$""")
        self.addCleanup(self.admin.execute, "DROP FUNCTION public.alert_fixture_cancel() CASCADE")
        self.admin.execute("""CREATE TRIGGER alert_fixture_cancel BEFORE INSERT ON public.alert_inbox
            FOR EACH ROW EXECUTE FUNCTION public.alert_fixture_cancel()""")
        worker = AlertWorker(self.repo, lambda: None)
        waits = [worker.step() for _ in range(5)]
        self.assertTrue(any(delay < 1 for delay in waits), waits)
        self.assertEqual(worker.step(), 5)
        self.assertIsNone(worker.failure)
        self.assertEqual([r['rule_name'] for r in self.messages()], ['healthy'])
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM public.alert_observations WHERE retry_at>statement_timestamp()"
        ).fetchone()[0], 1)
        self.client.app.state.alert_worker = worker
        self.addCleanup(delattr, self.client.app.state, 'alert_worker')
        status = self.client.get('/alerts/rules').json()
        self.assertEqual(status['processor'], 'waiting')
        self.assertEqual({r['definition']['name']: r['retry_pending'] for r in status['items']},
                         {'healthy': False, 'slow': True})
        self.assertTrue(all(r['gap_count'] == 0 for r in status['items']))
        self.admin.execute("DROP TRIGGER alert_fixture_cancel ON public.alert_inbox")
        self.admin.execute("UPDATE public.alert_observations SET retry_at=statement_timestamp()")
        restarted = AlertsRepository(self.db)
        self.assertEqual(restarted.process(('long',)), 1)
        self.assertEqual(restarted.process(('long',)), 0)
        self.assertEqual(sorted(r['rule_name'] for r in self.messages()), ['healthy', 'slow'])
        self.assertEqual(self.client.get('/alerts/rules').json()['processor'], 'ready')

    def test_subscribers(self):
        rule=self.add('channel.subscribers')
        for count in (99000,None,101000,105000):
            self.admin.execute("""INSERT INTO public.channel_snapshots(channel_id,recorded_at,subscribers,source)
                VALUES(%s,clock_timestamp(),%s,'youtube_channel_metadata_refresh')""",(self.channel,count))
        self.repo.process(('long',))
        self.assertEqual([i['subject'] for i in self.messages()],[self.channel])

    def test_subscribers_in_one_batch_are_delivered_per_channel(self):
        folder = str(uuid4())
        label = 'custom:' + str(uuid4())
        channels = ['UC' + str(i).zfill(22) for i in range(21)]
        members, outsider = channels[:-1], channels[-1]
        self.admin.execute("INSERT INTO public.folders(id,name) VALUES(%s,'Subscriber batch')", (folder,))
        self.admin.execute("INSERT INTO public.channel_labels(label_key,name,label_type) VALUES(%s,'Batch','custom')", (label,))
        self.admin.execute("""INSERT INTO public.channels(channel_id,user_work_id,folder_id,added_at)
            SELECT channel,gen_random_uuid(),CASE WHEN channel<>%s THEN %s::uuid END,clock_timestamp()
            FROM unnest(%s::text[]) AS channel""", (outsider, folder, channels))
        self.addCleanup(self.admin.execute, "DELETE FROM public.folders WHERE id=%s", (folder,))
        self.addCleanup(self.admin.execute, "DELETE FROM public.channel_labels WHERE label_key=%s", (label,))
        self.addCleanup(self.admin.execute, "DELETE FROM public.channels WHERE channel_id=ANY(%s)", (channels,))
        self.admin.execute("""INSERT INTO public.channel_collection_state(channel_id,user_work_id,owner_permission,is_hidden,control_epoch)
            SELECT channel_id,user_work_id,'accepted',false,1 FROM public.channels WHERE channel_id=ANY(%s)""", (channels,))
        self.admin.execute("""INSERT INTO public.channel_label_assignments(channel_id,label_key)
            SELECT channel,%s FROM unnest(%s::text[]) AS channel""", (label, members[1:]))
        folder_rule = self.add('channel.subscribers', scope={'kind':'folder','id':folder})['id']
        label_rule = self.add('channel.subscribers', scope={'kind':'label','id':label})['id']

        def capture(at, values):
            self.admin.execute("""INSERT INTO public.channel_snapshots(channel_id,recorded_at,subscribers,source)
                SELECT channel,%s,subscribers,'youtube_channel_metadata_refresh'
                FROM unnest(%s::text[],%s::bigint[]) AS batch(channel,subscribers)""", (at, channels, values))

        def pending():
            return self.admin.execute("""SELECT rule_id::text,subject FROM public.alert_observations""").fetchall()

        def replay(at, values):
            # Reinsert the very same source rows into the disposable database.
            self.admin.execute("DELETE FROM public.channel_snapshots WHERE recorded_at=%s AND channel_id=ANY(%s)",
                               (at, channels))
            with self.admin.transaction():
                self.admin.execute("SET LOCAL TIME ZONE 'Asia/Tokyo'")
                capture(at, values)

        start = self.admin.execute("SELECT clock_timestamp()").fetchone()[0]
        capture(start, [99000] * len(channels))
        self.assertEqual(pending(), [])
        self.repo.process(('long',))
        self.assertFalse(self.messages())
        at = self.admin.execute("SELECT clock_timestamp()").fetchone()[0]
        values = [100000] * len(channels)
        values[len(members) - 1] = 99999
        capture(at, values)
        replay(at, values)
        expected = [(folder_rule, c) for c in members[:-1]] + [(label_rule, c) for c in members[1:-1]]
        self.assertCountEqual(pending(), expected)
        self.repo.process(('long',))
        messages = self.messages()
        self.assertCountEqual([(str(m['rule_id']), m['subject']) for m in messages], expected)
        replay(at, values)
        AlertsRepository(self.db).process(('long',))
        self.assertEqual(self.messages(), messages)

    def test_subscriber_replay_after_an_older_duplicate_expires(self):
        self.add('channel.subscribers')
        insert = """INSERT INTO public.channel_snapshots(channel_id,recorded_at,subscribers,source)
                    VALUES(%s,%s,%s,'youtube_channel_metadata_refresh')"""

        def replay(at):
            self.admin.execute("DELETE FROM public.channel_snapshots WHERE channel_id=%s AND recorded_at=%s",
                               (self.channel, at))
            self.admin.execute(insert, (self.channel, at, 100000))

        for count in (99000, 100000):
            at = self.admin.execute("SELECT clock_timestamp()").fetchone()[0]
            self.admin.execute(insert, (self.channel, at, count))
        self.repo.process(('long',))
        first = int(self.messages()[0]['id'])
        # Represent the same instant using the session time zone.
        self.admin.execute("UPDATE public.alert_inbox SET source_key=observed_at::text WHERE id=%s", (first,))
        replay(at)
        self.repo.process(('long',))
        second = int(self.messages()[0]['id'])
        self.assertGreater(second, first)
        self.repo.read(second, ('long',))
        with self.admin.transaction():
            self.admin.execute("SET LOCAL TIME ZONE 'Asia/Tokyo'")
            self.admin.execute("UPDATE public.alert_inbox SET source_key=observed_at::text WHERE id=%s", (second,))
        # The first delivery used older evidence. Its expiry can precede the
        # second delivery's expiry even though the current snapshot is identical.
        self.admin.execute("""UPDATE public.alert_inbox SET
            previous_at=statement_timestamp()-interval '696 hours 1 second',
            expires_at=statement_timestamp()-interval '1 second' WHERE id=%s""", (first,))
        step, = (ROOT/'schema/steps').glob('*-channel-subscriber-alert-identity.sql')
        # The migration replaces the capture trigger. Replay the snapshot with the installed trigger.
        trigger = self.admin.execute("""SELECT pg_get_triggerdef(oid) FROM pg_trigger
            WHERE tgrelid='public.channel_snapshots'::regclass AND tgname='alert_channel_observations'""").fetchone()[0]
        with self.admin.transaction():
            self.admin.execute(step.read_text(), prepare=False)
            self.admin.execute("DROP TRIGGER alert_channel_observations ON public.channel_snapshots")
            self.admin.execute(trigger)
        self.assertEqual(self.admin.execute("SELECT count(*) FROM public.alert_inbox").fetchone()[0], 2)
        replay(at)
        self.repo.process(('long',))
        self.assertEqual([(int(m['id']), m['is_read']) for m in self.messages()], [(second, True)])


if __name__=='__main__':
    unittest.main()
