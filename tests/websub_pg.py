"""Disposable PG17: WebSub durable inbox, Hub pacing, aggregate Health, and the
event handler and count rule that apply until the discovery runtime is enabled."""
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import patch

from psycopg.types.json import Jsonb

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'application'), str(Path(__file__).resolve().parent)]
import research_query_pg as native
import websub
from db.base.postgres import DatabaseError, Operation
from db.channel.channel_read_policy import CHANNEL_CONTROL_SQL
from db.discovery_counts import observe_count_change
from db.websub import (WebSubRepository, WebSubManagerRepository, WebSubEventRepository, explained_increase,
                       BREAKER_FAILURES, MAX_ATTEMPTS, RAMP)

BASE = 'https://hooks.example.com/lc'


def notification(channel, video, kind='entry', key=None, at=None):
    published, updated = (at, None) if kind == 'entry' else (None, at)
    return websub.Notification(channel, video, kind, published, updated, key or websub._dedupe(channel, video, kind, video, None))


class WebSubPostgres(unittest.TestCase):
    setUpClass = classmethod(native.NativeQueryTests.setUpClass.__func__)
    tearDownClass = classmethod(native.NativeQueryTests.tearDownClass.__func__)
    refresh = classmethod(native.NativeQueryTests.refresh.__func__)

    def setUp(self):
        for table in ('websub_events', 'websub_journal', 'websub_subscriptions'):
            self.admin.execute(f'DELETE FROM public.{table}')
        self.admin.execute("UPDATE public.websub_hub_state SET hold_until=NULL,failures_in_row=0,breaker_open=false,ramp_stage=2")
        self.admin.execute('UPDATE public.discovery_runtime_control SET enabled=false WHERE singleton')
        self.eligible = [r[0] for r in self.admin.execute(
            'SELECT channel_id FROM (' + CHANNEL_CONTROL_SQL + ') p WHERE p.typed_runtime_eligible ORDER BY 1').fetchall()]
        self.assertTrue(self.eligible, 'fixture has eligible channels')
        self.channel = self.eligible[0]

    def scalar(self, sql, *params):
        row = self.admin.execute(sql, params).fetchone()
        return None if row is None else row[0]

    @contextmanager
    def discovery_enabled(self):
        saved = self.scalar('SELECT first_activated_at FROM public.discovery_runtime_control WHERE singleton')
        self.admin.execute("""UPDATE public.discovery_runtime_control SET enabled=true,
            first_activated_at=coalesce(first_activated_at,clock_timestamp()) WHERE singleton""")
        try:
            yield
        finally:
            self.admin.execute('UPDATE public.discovery_runtime_control SET enabled=false,first_activated_at=%s WHERE singleton', (saved,))

    def event_state(self, video):
        return self.admin.execute("""SELECT status,work_phase,attempts,outcome FROM public.websub_events
            WHERE video_id=%s ORDER BY event_id DESC LIMIT 1""", (video,)).fetchone()

    def test_01_plan_follows_eligibility_and_callback(self):
        manager = WebSubManagerRepository(self.db)
        manager.plan(BASE)
        rows = self.admin.execute('SELECT channel_id,state,topic,callback_base,generation FROM public.websub_subscriptions ORDER BY 1').fetchall()
        self.assertEqual([r[0] for r in rows], self.eligible)
        self.assertTrue(all(r[1] == 'pending' and r[2] == websub.topic_url(r[0]) and r[3] == BASE and r[4] == 1 for r in rows))
        # A new callback base starts a new generation; an unchanged plan changes nothing.
        manager.plan(BASE)
        self.assertEqual(self.scalar('SELECT max(generation) FROM public.websub_subscriptions'), 1)
        manager.plan(BASE + '2')
        self.assertEqual(self.scalar('SELECT min(generation) FROM public.websub_subscriptions'), 2)
        # Hidden channel: a never-sent pending row is closed without a Hub request.
        self.admin.execute('UPDATE public.channel_collection_state SET is_hidden=true WHERE channel_id=%s', (self.channel,))
        try:
            manager.plan(BASE + '2')
            self.assertEqual(self.scalar('SELECT state FROM public.websub_subscriptions WHERE channel_id=%s', self.channel), 'unsubscribed')
        finally:
            self.admin.execute('UPDATE public.channel_collection_state SET is_hidden=false WHERE channel_id=%s', (self.channel,))
        manager.plan(BASE + '2')
        self.assertEqual(self.scalar('SELECT state FROM public.websub_subscriptions WHERE channel_id=%s', self.channel), 'pending')

    def test_02_reserve_settle_ramp_hold_breaker(self):
        manager = WebSubManagerRepository(self.db)
        manager.plan(BASE)
        self.admin.execute('UPDATE public.websub_hub_state SET ramp_stage=0')
        row, admission = manager.reserve()
        self.assertTrue(admission.allowed)
        self.assertEqual(self.scalar("SELECT count(*) FROM public.websub_journal WHERE outcome='sending'"), 1)
        manager.settle(row, websub.HubResult('accepted', 202, None, 0.4))
        # Canary: nothing more until the first subscription is verified.
        self.assertEqual(manager.reserve()[1].reason, 'ramp_wait')
        WebSubRepository(self.db).verification(row['channel_id'], row['generation'], 'subscribe', 864000, outcome='verified')
        state, lease, renew_hours = self.admin.execute("""SELECT state,lease_seconds,extract(epoch FROM renew_at-verified_at)/3600
            FROM public.websub_subscriptions WHERE channel_id=%s""", (row['channel_id'],)).fetchone()
        self.assertEqual((state, lease), ('verified', 864000))
        self.assertTrue(0.70 * 240 <= renew_hours <= 0.80 * 240 + 0.01)
        if len(self.eligible) > 1:
            row2, admission = manager.reserve()
            self.assertTrue(admission.allowed, admission.reason)
            self.assertEqual(self.scalar('SELECT ramp_stage FROM public.websub_hub_state'), 1)
            # 429 with Retry-After: Hub-wide hold and a later retry for this channel.
            manager.settle(row2, websub.HubResult('http_429', 429, 300, 0.2))
            hold, failures = self.admin.execute('SELECT extract(epoch FROM hold_until-clock_timestamp()),failures_in_row FROM public.websub_hub_state').fetchone()
            self.assertTrue(250 < hold <= 300)
            self.assertEqual(failures, 1)
            self.assertEqual(manager.reserve()[1].reason, 'hub_hold')
        # Repeated failures open the breaker until reset.
        self.admin.execute("UPDATE public.websub_hub_state SET hold_until=NULL,ramp_stage=2")
        self.admin.execute("UPDATE public.websub_subscriptions SET next_try_at=clock_timestamp(),attempts=0 WHERE state='pending'")
        opened = False
        for _ in range(BREAKER_FAILURES):
            self.admin.execute("UPDATE public.websub_hub_state SET hold_until=NULL")
            row, admission = manager.reserve()
            if row is None:
                break
            opened = manager.settle(row, websub.HubResult('http_500', 500, None, 0.1))
        if len(self.eligible) >= BREAKER_FAILURES:
            self.assertTrue(opened)
            self.assertEqual(manager.reserve()[1].reason, 'breaker_open')
            manager.reset_breaker()
            self.assertNotEqual(manager.reserve()[1].reason, 'breaker_open')
        self.assertEqual(RAMP[0], (1, 1))

    def test_02b_open_breaker_retries_once_after_six_hours(self):
        manager = WebSubManagerRepository(self.db)
        manager.plan(BASE)
        self.admin.execute("""UPDATE public.websub_hub_state SET breaker_open=true,failures_in_row=5,
            updated_at=clock_timestamp()-interval '5 hours'""")
        self.assertEqual(manager.reserve()[1].reason, 'breaker_open')
        self.admin.execute("UPDATE public.websub_hub_state SET updated_at=clock_timestamp()-interval '7 hours'")
        row, admission = manager.reserve()
        self.assertTrue(admission.allowed)
        self.assertEqual(self.scalar("SELECT count(*) FROM public.websub_journal WHERE outcome='breaker_retry'"), 1)
        self.assertTrue(manager.settle(row, websub.HubResult('http_500', 500, None, 0.1)))  # reopens at once
        self.assertEqual(manager.reserve()[1].reason, 'breaker_open')

    def test_02c_ramp_waits_but_still_reasks_the_unconfirmed_request(self):
        manager = WebSubManagerRepository(self.db)
        manager.plan(BASE)
        self.admin.execute('UPDATE public.websub_hub_state SET ramp_stage=0')
        row, _ = manager.reserve()
        manager.settle(row, websub.HubResult('accepted', 202, None, 0.2))
        self.assertEqual(manager.reserve()[1].reason, 'ramp_wait')
        self.admin.execute("UPDATE public.websub_subscriptions SET next_try_at=clock_timestamp() WHERE channel_id=%s", (row['channel_id'],))
        again, admission = manager.reserve()
        self.assertTrue(admission.allowed)
        self.assertEqual((again['channel_id'], again['attempts']), (row['channel_id'], 2))
        self.assertEqual(self.scalar("SELECT count(*) FROM public.websub_subscriptions WHERE requested_at IS NOT NULL"), 1)

    def test_03_no_answer_is_not_resent_at_once(self):
        manager = WebSubManagerRepository(self.db)
        manager.plan(BASE)
        row, _ = manager.reserve()
        manager.settle(row, websub.HubResult('timeout', None, None, 120.0))
        wait = self.scalar('SELECT extract(epoch FROM next_try_at-clock_timestamp()) FROM public.websub_subscriptions WHERE channel_id=%s', row['channel_id'])
        self.assertTrue(1700 < wait <= 1800)
        self.assertTrue(self.scalar('SELECT hold_until>clock_timestamp() FROM public.websub_hub_state'))

    def test_04_delivery_dedupes_and_journals(self):
        repository = WebSubRepository(self.db)
        events = [notification(self.channel, 'abcdefghijk'), notification(self.channel, 'abcdefghijk')]
        self.assertEqual(repository.delivery(self.channel, events, size=500), 1)
        self.assertEqual(repository.delivery(self.channel, events[:1], size=500), 0)
        self.assertEqual(self.scalar('SELECT count(*) FROM public.websub_events'), 1)
        self.assertEqual(self.admin.execute('SELECT status,attempts,outcome FROM public.websub_events').fetchone(),
                         ('pending', 0, None))
        self.assertEqual(self.scalar("SELECT count(*) FROM public.websub_journal WHERE kind='delivery' AND outcome='accepted'"), 2)

    def test_05_events_make_existing_work_due(self):
        manager = WebSubManagerRepository(self.db)
        manager.plan(BASE)
        channel = self.channel
        # A known video is refreshed only for a channel past its initial load
        # (collection mode 'active'), exactly as the existing priority rule says.
        job = str(uuid.uuid4())
        self.admin.execute("""INSERT INTO public.operator_jobs(id,job_type,channel_id,status,phase,initial_scope_version,
            initial_video_types,max_items,max_pages,max_quota_units,accepted_epoch,error_count)
            VALUES(%s,'channel_initial_long_inventory',%s,'completed','verify',0,ARRAY['long'],2500,50,100,1,0)""", (job, channel))
        self.admin.execute('UPDATE public.channel_collection_state SET initial_job_id=%s WHERE channel_id=%s', (job, channel))
        self.assertEqual(self.scalar('SELECT collection_mode FROM (' + CHANNEL_CONTROL_SQL + ') p WHERE channel_id=%s', channel), 'active')
        known = self.scalar("""SELECT video_id FROM public.video_inventory WHERE channel_id=%s AND video_type='long' ORDER BY 1 LIMIT 1""", channel)
        self.admin.execute("""INSERT INTO public.video_collection_state(video_id,channel_id,video_type)
            VALUES(%s,%s,'long') ON CONFLICT DO NOTHING""", (known, channel))
        self.admin.execute("""UPDATE public.video_collection_state SET metadata_due_at=clock_timestamp()+interval '9 days',
            next_due_at=clock_timestamp()+interval '9 days',lease_token=NULL,paused_reason=NULL WHERE video_id=%s""", (known,))
        self.admin.execute("""UPDATE public.channel_playlist_state SET runtime_enabled=true,status='ready',priority=0,
            next_due_at=clock_timestamp()+interval '9 days' WHERE channel_id=%s""", (channel,))
        # The stream playlist is proven missing: the guard keeps its own schedule.
        self.admin.execute("UPDATE public.channel_playlist_state SET inventory_status='playlist_not_found' WHERE channel_id=%s AND video_type='stream'", (channel,))
        try:
            repository = WebSubRepository(self.db)
            now = self.scalar('SELECT clock_timestamp()')
            self.admin.execute("UPDATE public.channels SET channel_video_count_changed_at=%s WHERE channel_id=%s",
                               (now - timedelta(minutes=5), channel))
            # A fresh upload counts; a removal dated before the last count change does not.
            repository.delivery(channel, [notification(channel, 'zz000000001', at=now), notification(channel, known),
                                          notification(channel, known, 'deleted', at=now - timedelta(minutes=10)),
                                          notification(channel, 'zz000000002', 'deleted')], size=1)
            self.assertEqual(WebSubEventRepository(self.db).process(), 4)
            outcomes = dict(self.admin.execute('SELECT video_id||kind,outcome FROM public.websub_events').fetchall())
            self.assertEqual(outcomes, {'zz000000001entry': 'discovery_requested', known + 'entry': 'refresh_requested',
                                        known + 'deleted': 'deletion_check_requested', 'zz000000002deleted': 'unknown_video'})
            due = dict(self.admin.execute('SELECT video_type,next_due_at<=clock_timestamp() FROM public.channel_playlist_state WHERE channel_id=%s', (channel,)).fetchall())
            self.assertEqual(due, {'long': True, 'short': True, 'stream': False})
            self.assertTrue(self.scalar('SELECT metadata_due_at<=clock_timestamp() FROM public.video_collection_state WHERE video_id=%s', known))
            self.assertEqual(self.scalar('SELECT pending_delta FROM public.websub_subscriptions WHERE channel_id=%s', channel), 1)
            # Every handled envelope is closed for the discovery owner as well.
            self.assertEqual(self.scalar("SELECT count(*) FROM public.websub_events WHERE status='pending' OR work_phase<>'settled'"), 0)
            # A later update of the still unknown video does not count it twice.
            repository.delivery(channel, [notification(channel, 'zz000000001', key='f' * 64)], size=1)
            WebSubEventRepository(self.db).process()
            self.assertEqual(self.scalar("SELECT outcome FROM public.websub_events WHERE dedupe_key=%s", 'f' * 64), 'already_requested')
            self.assertEqual(WebSubEventRepository(self.db).process(), 0)
            self.assertEqual(self.scalar('SELECT pending_delta FROM public.websub_subscriptions WHERE channel_id=%s', channel), 1)
        finally:
            self.admin.execute("UPDATE public.channel_playlist_state SET inventory_status='completed' WHERE channel_id=%s AND video_type='stream'", (channel,))
            self.admin.execute('UPDATE public.channel_collection_state SET initial_job_id=NULL WHERE channel_id=%s', (channel,))
            self.admin.execute('DELETE FROM public.operator_jobs WHERE id=%s', (job,))

    def test_05b_handler_takes_only_untouched_events_and_stops_at_activation(self):
        WebSubManagerRepository(self.db).plan(BASE)
        events, video = WebSubEventRepository(self.db), 'zz000000003'
        WebSubRepository(self.db).delivery(self.channel, [notification(self.channel, video)], size=1)
        # An enabled discovery runtime owns every envelope: the handler leaves it untouched.
        with self.discovery_enabled():
            self.assertEqual(events.process(), 0)
            self.assertEqual(self.event_state(video), ('pending', 'metadata_wait', 0, None))
        # So does a row the discovery owner has claimed, moved on or paused.
        for touched, untouched in (
                ("claim_token=gen_random_uuid(),claim_expires_at=clock_timestamp()+interval '5 minutes'",
                 'claim_token=NULL,claim_expires_at=NULL'),
                ("work_phase='membership'", "work_phase='metadata_wait'")):
            self.admin.execute('UPDATE public.websub_events SET ' + touched + ' WHERE video_id=%s', (video,))
            self.assertEqual(events.process(), 0)
            self.admin.execute('UPDATE public.websub_events SET ' + untouched + ' WHERE video_id=%s', (video,))
        self.assertEqual(events.process(), 1)
        self.assertEqual(self.event_state(video), ('done', 'settled', 1, 'discovery_requested'))

    def test_05c_failed_effect_retries_then_closes(self):
        WebSubManagerRepository(self.db).plan(BASE)
        events, video = WebSubEventRepository(self.db), 'zz000000004'
        now = self.scalar('SELECT clock_timestamp()')
        WebSubRepository(self.db).delivery(self.channel, [notification(self.channel, video, at=now)], size=1)
        with patch('db.inventory.inventory_priority.prioritize_typed_inventory_channels',
                   side_effect=DatabaseError('effect_broken')):
            self.assertEqual(events.process(), 1)
            self.assertEqual(self.event_state(video), ('pending', 'metadata_wait', 1, 'effect_broken'))
            self.assertEqual(events.process(), 0)   # the retry waits its turn
            self.admin.execute('UPDATE public.websub_events SET attempts=%s,next_try_at=clock_timestamp() WHERE video_id=%s',
                               (MAX_ATTEMPTS - 1, video))
            self.assertEqual(events.process(), 1)
        self.assertEqual(self.event_state(video), ('failed', 'settled', MAX_ATTEMPTS, 'effect_broken'))
        # The failed effect was rolled back with its savepoint: no tally is left behind.
        self.assertEqual(self.scalar('SELECT pending_delta FROM public.websub_subscriptions WHERE channel_id=%s', self.channel), 0)

    def test_06_count_rule(self):
        channel = self.channel
        WebSubManagerRepository(self.db).plan(BASE)
        self.admin.execute("""UPDATE public.websub_subscriptions SET state='verified',expires_at=clock_timestamp()+interval '9 days',
            pending_delta=2,pending_since=clock_timestamp() WHERE channel_id=%s""", (channel,))
        op = Operation('websub_test', statement_timeout_ms=3000, lock_timeout_ms=500, total_timeout_ms=5000)
        with self.db.transaction(op) as tx:
            self.assertFalse(explained_increase(tx, channel, 1))      # mismatch: normal discovery, tally cleared
        self.assertEqual(self.scalar('SELECT pending_delta FROM public.websub_subscriptions WHERE channel_id=%s', channel), 0)
        self.admin.execute("UPDATE public.websub_subscriptions SET pending_delta=1,pending_since=clock_timestamp() WHERE channel_id=%s", (channel,))
        with self.db.transaction(op) as tx:
            self.assertTrue(explained_increase(tx, channel, 1))
        self.admin.execute("UPDATE public.websub_subscriptions SET pending_delta=1,pending_since=clock_timestamp()-interval '25 hours' WHERE channel_id=%s", (channel,))
        with self.db.transaction(op) as tx:
            self.assertFalse(explained_increase(tx, channel, 1))      # stale announcement
        self.admin.execute("UPDATE public.websub_subscriptions SET pending_delta=1,pending_since=clock_timestamp(),state='pending' WHERE channel_id=%s", (channel,))
        with self.db.transaction(op) as tx:
            self.assertFalse(explained_increase(tx, channel, 1))      # not verified

    def test_06b_announced_growth_is_not_signalled_twice_before_activation(self):
        channel = self.channel
        WebSubManagerRepository(self.db).plan(BASE)
        lists = self.scalar('SELECT count(*) FROM public.channel_playlist_state WHERE channel_id=%s', channel)
        self.assertTrue(lists)
        base = self.scalar('SELECT clock_timestamp()') - timedelta(hours=1)
        columns = ('count_reconcile_token,count_reconcile_previous,count_reconcile_current,'
                   'count_reconcile_source_checked_at,count_reconcile_observed_at,count_reconcile_epoch')
        saved = self.admin.execute('SELECT ' + columns + ' FROM public.channels WHERE channel_id=%s', (channel,)).fetchone()
        heads = self.admin.execute("""SELECT video_type,head_signal_token,head_next_due_at FROM public.channel_playlist_state
            WHERE channel_id=%s""", (channel,)).fetchall()

        def tally(delta):
            self.admin.execute("""UPDATE public.websub_subscriptions SET state='verified',expires_at=clock_timestamp()+interval '9 days',
                pending_delta=%s,pending_since=clock_timestamp() WHERE channel_id=%s""", (delta, channel))
            self.admin.execute('UPDATE public.channel_playlist_state SET head_signal_token=NULL WHERE channel_id=%s', (channel,))

        def count(old, new, minute):
            at = base + timedelta(minutes=minute)
            with self.db.transaction(Operation('websub_count_fixture')) as tx:
                tx.fetch_one('SELECT channel_id FROM public.channels WHERE channel_id=%s FOR UPDATE', (channel,))
                observe_count_change(
                    tx, Jsonb(dict(video_count=old, channel_metadata_checked_at=(at - timedelta(seconds=30)).isoformat())),
                    Jsonb(dict(channel_id=channel, video_count=new, channel_metadata_checked_at=at.isoformat(),
                               channel_metadata_status='ok')))
            signalled = self.scalar("""SELECT count(*) FROM public.channel_playlist_state p JOIN public.channels c USING(channel_id)
                WHERE c.channel_id=%s AND p.head_signal_token=c.count_reconcile_token""", channel)
            return signalled, self.scalar('SELECT pending_delta FROM public.websub_subscriptions WHERE channel_id=%s', channel)

        try:
            tally(1)
            self.assertEqual(count(10, 11, 1), (0, 0))          # announced in full: the lists are not read again
            self.assertEqual(count(11, 12, 2), (lists, 0))      # nothing announced: every list head is due
            tally(2)
            self.assertEqual(count(12, 13, 3), (lists, 0))      # a mismatch: heads due, tally cleared
            tally(1)
            self.assertEqual(count(13, 12, 4), (0, 1))          # a decrease signals nothing and keeps the tally
            with self.discovery_enabled():
                self.assertEqual(count(12, 13, 5), (lists, 1))  # enabled runtime: the tally is never read
        finally:
            self.admin.execute('UPDATE public.channels SET (' + columns + ')=(%s,%s,%s,%s,%s,%s) WHERE channel_id=%s',
                               (*saved, channel))
            for video_type, token, due in heads:
                self.admin.execute("""UPDATE public.channel_playlist_state SET head_signal_token=%s,head_next_due_at=%s
                    WHERE channel_id=%s AND video_type=%s""", (token, due, channel, video_type))

    def test_07_verification_modes_and_clean(self):
        repository = WebSubRepository(self.db)
        WebSubManagerRepository(self.db).plan(BASE)
        channel = self.channel
        self.assertEqual(repository.verification(channel, 1, 'unsubscribe', None, outcome='verified'), 'state_mismatch')
        self.assertEqual(repository.verification(channel, 1, 'subscribe', None, outcome='verified'), 'verified')
        self.assertEqual(self.scalar('SELECT lease_seconds FROM public.websub_subscriptions WHERE channel_id=%s', channel), 432000)
        self.admin.execute("UPDATE public.websub_subscriptions SET state='unsubscribing' WHERE channel_id=%s", (channel,))
        self.assertEqual(repository.verification(channel, 1, 'unsubscribe', None, outcome='verified'), 'verified')
        self.assertEqual(self.scalar('SELECT state FROM public.websub_subscriptions WHERE channel_id=%s', channel), 'unsubscribed')
        self.admin.execute("INSERT INTO public.websub_journal(at,kind,outcome) VALUES(clock_timestamp()-interval '91 days','delivery','accepted')")
        self.assertEqual(WebSubManagerRepository(self.db).clean(), 1)

    def test_08_health_totals_read_as_lic_reader(self):
        # Health reads inside an HTTP request, where every read is lic_reader.
        # That role reads no public.websub_* table, only the totals of readable.websub_health.
        from db.base.postgres import DatabaseError
        from db.base.read_context import http_method_scope, http_read_scope
        from db.websub import health
        WebSubManagerRepository(self.db).plan(BASE)
        self.admin.execute("""UPDATE public.websub_subscriptions SET state='verified',
            expires_at=clock_timestamp()+interval '2 hours' WHERE channel_id=%s""", (self.channel,))
        WebSubRepository(self.db).delivery(self.channel, [notification(self.channel, 'abcdefghijk')], size=500)
        self.admin.execute("INSERT INTO public.websub_journal(at,kind,outcome) VALUES(clock_timestamp(),'hub_request','accepted')")
        with http_method_scope('GET'), http_read_scope():
            row = health(self.db)
            with self.assertRaises(DatabaseError) as refused:
                self.db.read_row('websub_reader_probe', 'SELECT count(*) AS n FROM public.websub_subscriptions')
        self.assertEqual(getattr(refused.exception, 'code', None), '42501')
        pending = len(self.eligible) - 1
        self.assertEqual(row['subscriptions'], {'verified': 1, **({'pending': pending} if pending else {})})
        self.assertEqual((row['expiring_24h'], row['deliveries_24h'], row['events_pending']), (1, 1, 1))
        self.assertEqual((row['events_24h'], row['hub_requests_24h']), ({'pending': 1}, {'accepted': 1}))
        self.assertEqual((row['hold_until'], row['breaker_open'], row['ramp_stage']), (None, False, 2))
        self.assertTrue(row['last_delivery_at'])
        # The same answer as the service role's own read outside a request.
        self.assertEqual(health(self.db), row)


if __name__ == '__main__':
    unittest.main(verbosity=2)
