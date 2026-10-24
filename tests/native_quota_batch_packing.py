"""Explicit <=1000-row PG17 proof: real mixed claims, feeder and processor.

Run only inside the disposable, network-none native wrapper. No provider I/O.
Source writes use the real counter builder and completion transaction owner.
"""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Lock
from uuid import uuid4
import argparse
import json
import os
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
from schema25_regressions.fixtures import Fixture, admission, dsns
from collection.auto_pass import AutoPassState, CommonCandidateCursor
from collection.common_feeder import CommonAutoFeeder
from collection.dispatch import Coordinator, DispatchBlocked
from collection.video_payloads import build_video_counter_payload
from db.base.postgres import DatabaseUnavailable, PostgresDatabase
from db.videos.video_collection import VideoCollectionRepository
from video_collection_service import VideoCollectionService

CASES = ('50x1', '100x1', '10x5', 'fair_refill', 'locked_row', 'claim_race',
         'claim_ack_loss', 'completion_ack_loss')
MAX_ROWS = 1000
WORK_SECONDS = 420


def channel(index):
    return 'UC' + str(index).zfill(22)


def seed(f, sizes):
    """Only synthetic inputs; native channel owner, explicit counters-only debt."""
    assert 1 <= len(sizes) <= 100 and sum(sizes) <= 150
    identities = {}
    for index, count in enumerate(sizes, 1):
        identity = f.channel(channel(index))
        for item in range(count):
            video = f'q{index:05d}{item:05d}'
            identities[video] = identity
    old, due, future = f.now-timedelta(days=2), f.now-timedelta(minutes=1), f.now+timedelta(days=2)
    with f.admin.transaction():
        for video, identity in identities.items():
            f.rows("INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at) VALUES(%s,%s,'long',%s)",
                   (video, identity, old))
            f.rows("""INSERT INTO public.videos(video_id,channel_id,published_at,checked_at,metadata_checked_at,
                availability_checked_at,availability_status,privacy_status,upload_status,views,like_count,comment_count)
                VALUES(%s,%s,%s,%s,%s,%s,'public','public','processed',1,1,0)""",
                   (video, identity, old, old, old, old))
            f.rows("""INSERT INTO public.video_collection_state(video_id,channel_id,video_type,
                hydration_needed,profile_metadata_version,stats_due_at,metadata_due_at,next_due_at,
                stats_source_checked_at,metadata_source_checked_at)
                VALUES(%s,%s,'long',false,1,%s,%s,%s,%s,%s)""",
                   (video, identity, due, future, due, old, old))
    f.observe()
    return identities


class ObservedRepository(VideoCollectionRepository):
    """Calls the unchanged real owners; only post-COMMIT ACK loss is injected."""
    def __init__(self, database, *, loss=None):
        super().__init__(database)
        self.loss, self.claims, self.completions, self.calls = loss, [], 0, 0

    def claim_due_batch(self, **kwargs):
        self.calls += 1
        result = super().claim_due_batch(**kwargs)
        self.claims.append(result)
        if self.loss == 'claim':
            raise DatabaseUnavailable('database_connection_failed', code='08006', outcome_unknown=True)
        return result

    def complete_counters(self, rows):
        result = super().complete_counters(rows)
        self.completions += 1
        if self.loss == 'completion':
            raise DatabaseUnavailable('database_connection_failed', code='08006', outcome_unknown=True)
        return result


def drive(f, identities, *, loss=None, concurrency=2):
    repository = ObservedRepository(f.database, loss=loss)
    sent, guard = [], Lock()
    def forbidden(*args, **kwargs):
        raise AssertionError('unexpected_provider_bucket')
    def counters(ids, reason):
        assert 1 <= len(ids) <= 50 and len(ids) == len(set(ids))
        assert set(ids) <= identities.keys()
        with guard:
            sent.append(tuple(ids))
        return dict(items=[dict(id=video, views=100, like_count=2, comment_count=0) for video in ids],
                    summary=dict(failed_ids=[]))
    service = VideoCollectionService(repository, fetch_full=forbidden, fetch_metadata=forbidden,
        fetch_counters=counters, build_full_payload=forbidden, build_counter_payload=build_video_counter_payload,
        is_quota_exhausted=lambda error: False)
    state = AutoPassState(len(identities), concurrency=concurrency)
    feeder = CommonAutoFeeder(service, state=state, cursor=CommonCandidateCursor(),
        worker_id='quota02_native', require_ready=f.gate.require_ready)
    blocked = False
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        coordinator = Coordinator(executor, concurrency)
        for _ in range(5000):
            f.check(time.monotonic() < f.deadline, 'native_deadline')
            feeder.offer_to(coordinator)
            try:
                coordinator.step()
            except DispatchBlocked:
                blocked = True
                break
            snapshot = coordinator.snapshot()
            f.check(snapshot.ready <= 2*concurrency and snapshot.held <= concurrency, 'coordinator_bounds')
            if feeder.exhausted and not snapshot.ready and not snapshot.held:
                break
            coordinator.wait(0.01)
        else:
            raise AssertionError('feeder_not_drained')
    if loss is None:
        f.check(not blocked and feeder.exhausted, 'feeder_drained')
        f.check(feeder.summary['updated'] == len(identities), 'all_updates_acknowledged')
        f.check(feeder.summary['metadata_requests'] == 0, 'counter_bucket_only')
        f.check(state.claimed == len(identities) and state.held == 0, 'pass_accounting')
        feeder.finish_pass(coordinator)
    else:
        f.check(blocked and coordinator.snapshot().held == 1, 'unknown_ack_holds_credit')
        f.check(feeder.offer_to(coordinator) == 0, 'unknown_ack_blocks_new_work')
        previous = (repository.calls, len(sent), repository.completions)
        try:
            coordinator.step()
        except DispatchBlocked:
            pass
        else:
            raise AssertionError('unknown_ack_not_blocked')
        f.check(previous == (repository.calls, len(sent), repository.completions), 'unknown_ack_no_retry')
    f.observe()
    return feeder, repository, sent


def settled(f, count):
    facts = f.scalar("""SELECT jsonb_build_object(
        'videos',(SELECT count(*) FROM public.videos WHERE views=100),
        'snapshots',(SELECT count(*) FROM public.video_snapshots),
        'released',(SELECT count(*) FROM public.video_collection_state
                    WHERE lease_token IS NULL AND next_due_at>clock_timestamp()))""")
    f.check(facts == dict(videos=count, snapshots=count, released=count), 'native_counter_settlement')


def packed(f, sizes, expected):
    identities = seed(f, sizes)
    feeder, repository, sent = drive(f, identities)
    f.check(len(sent) == expected and feeder.summary['batch_requests'] == expected, 'packed_request_count')
    f.check(sorted(len(batch) for batch in sent) == [50]*expected, 'packed_full_batches')
    f.check(len(set(video for batch in sent for video in batch)) == sum(sizes), 'each_video_once')
    f.check(all(0 <= claim['inspected_count'] <= 1000 for claim in repository.claims), 'raw_window_bound')
    settled(f, len(identities))
    return dict(channels=len(sizes), videos=sum(sizes), requests=len(sent), request_sizes=[len(x) for x in sent])


def fair_refill(f):
    from db.videos.video_scheduled_claims import _MIXED_WINDOW
    identities = seed(f, [60]+[1]*48+[0])
    parameters = dict(channels=[channel(index) for index in range(1, 51)], types=['long']*50, scope_limit=20)
    raw = f.rows(_MIXED_WINDOW, parameters)[0]
    f.check(len(raw['ids']) == len(set(raw['ids'])) == 108 and raw['scan_exhausted'], 'raw_refill_covers_large_small_empty')
    plan = f.rows('EXPLAIN (ANALYZE, FORMAT JSON) '+_MIXED_WINDOW, parameters)[0]
    indexes = set()
    def visit(node):
        if isinstance(node, dict):
            if 'Index Name' in node: indexes.add(node['Index Name'])
            for value in node.values(): visit(value)
        elif isinstance(node, list):
            for value in node: visit(value)
    visit(plan)
    feeder, repository, sent = drive(f, identities, concurrency=1)
    f.check([len(batch) for batch in sent] == [50, 50, 8], 'refill_packed')
    first_channels = {identities[video] for video in sent[0]}
    f.check(first_channels == {channel(index) for index in range(1, 50)}, 'small_channels_not_starved')
    settled(f, 108)
    return dict(channels=50, videos=108, requests=3, first_batch_channels=len(first_channels),
                raw_rows=len(raw['ids']), raw_complete=raw['scan_exhausted'], native_plan_indexes=sorted(indexes))

def claim(f, scopes, *, limit=50, repository=None):
    return (repository or VideoCollectionRepository(f.database)).claim_due_batch(
        limit=limit, worker_id='quota02_native', run_id=str(uuid4()), lease_seconds=90, scopes=tuple(scopes))


def locked_row(f):
    identities = seed(f, [2, 2])
    blocked_video = next(iter(identities))
    scopes = [dict(channel_id=channel(index), video_type='long') for index in (1, 2)]
    with f.admin.transaction():
        f.rows('SELECT video_id FROM public.video_collection_state WHERE video_id=%s FOR UPDATE', (blocked_video,))
        batch = claim(f, scopes)
        f.check({row.video_id for row in batch['rows']} == set(identities)-{blocked_video}, 'locked_row_skipped')
    later = claim(f, scopes)
    f.check([row.video_id for row in later['rows']] == [blocked_video], 'unlocked_row_claimable')
    return dict(claimed_while_locked=3, claimed_after_unlock=1, provider_requests=0)


def claim_race(f):
    identities = seed(f, [10, 10])
    scopes = [dict(channel_id=channel(index), video_type='long') for index in (1, 2)]
    barrier = Barrier(2, timeout=5)
    def contender(database):
        barrier.wait()
        return claim(f, scopes, limit=10, repository=VideoCollectionRepository(database))
    with PostgresDatabase(f.database.config, cadence_snapshot=f.database.cadence_snapshot) as other:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(copy_context().run, contender, database) for database in (f.database, other)]
            batches = [future.result(timeout=20) for future in futures]
    first, second = ({row.video_id for row in batch['rows']} for batch in batches)
    f.check(first.isdisjoint(second) and first|second <= identities.keys(), 'concurrent_claims_disjoint')
    later = {row.video_id for row in claim(f, scopes)['rows']}
    f.check(not (first|second)&later and first|second|later == identities.keys(), 'race_complete_without_duplicate')
    f.check(f.scalar('SELECT max(attempt_count) FROM public.video_collection_state') == 1, 'one_native_lease_per_video')
    return dict(contender_claims=[len(first), len(second)], remainder=len(later), provider_requests=0)


def ack_loss(f, phase):
    identities = seed(f, [1, 1])
    feeder, repository, sent = drive(f, identities, loss=phase, concurrency=1)
    f.check(repository.calls == 1, 'one_claim_attempt')
    if phase == 'claim':
        f.check(not sent and feeder.state.paused and feeder.state.stop_reason == 'claim_unknown', 'claim_ack_no_provider')
        f.check(f.scalar('SELECT count(*) FROM public.video_collection_state WHERE lease_token IS NOT NULL') == 2,
                'claim_committed_before_ack_loss')
    else:
        f.check(len(sent) == 1 and repository.completions == 1, 'one_provider_and_completion')
        settled(f, 2)
        f.check(feeder.summary['updated'] == 0, 'unknown_completion_not_acknowledged')
    return dict(phase=phase, claim_attempts=repository.calls, requests=len(sent), completion_commits=repository.completions)


def safe_error(error):
    code = str(error) if isinstance(error, AssertionError) else None
    if code and (len(code)>96 or not code.replace('_','').isalnum()):
        code = None
    return dict(error_class=type(error).__name__, check_code=code,
        trace=[dict(file=Path(frame.filename).name, line=frame.lineno, function=frame.name)
               for frame in traceback.extract_tb(error.__traceback__)[-5:]])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-proof', action='store_true')
    args = parser.parse_args(argv)
    if not args.run_proof:
        print(json.dumps(dict(cases=CASES, max_rows=MAX_ROWS, work_seconds=WORK_SECONDS, native_executed=False)))
        return 0
    if os.environ.get('LIC_SCHEMA_CI_MAX_ROWS') != str(MAX_ROWS):
        parser.error('Explicit 1000-row disposable scope required.')
    report = dict(status='failed', cases=[], max_rows=MAX_ROWS, peak_rows=0, youtube_requests=0,
                  limitations=['Synthetic provider; no live YouTube/API semantics acceptance.',
                               'Unknown ACK injected after confirmed COMMIT, not a physical connection cut.'])
    fixture = None
    try:
        with admission() as gate:
            fixture = Fixture(*dsns(), gate)
            fixture.deadline = time.monotonic()+WORK_SECONDS
            try:
                for name in CASES:
                    fixture.reset()
                    item = dict(case=name, status='failed')
                    report['cases'].append(item)
                    try:
                        if name == '50x1': details = packed(fixture, [1]*50, 1)
                        elif name == '100x1': details = packed(fixture, [1]*100, 2)
                        elif name == '10x5': details = packed(fixture, [5]*10, 1)
                        elif name == 'fair_refill': details = fair_refill(fixture)
                        elif name == 'locked_row': details = locked_row(fixture)
                        elif name == 'claim_race': details = claim_race(fixture)
                        else: details = ack_loss(fixture, 'claim' if name == 'claim_ack_loss' else 'completion')
                        rows = fixture.observe()
                        report['peak_rows'] = max(report['peak_rows'], rows)
                        item.update(status='passed', aggregate_rows=rows, **details)
                        print(json.dumps(dict(stage='case', **item)), flush=True)
                    except Exception as error:
                        item.update(safe_error(error))
                        raise
            finally:
                fixture.close()
                report['fixture_restored'] = True
            report['status'] = 'passed'
    except Exception as error:
        report.update(safe_error(error))
    print(json.dumps(report, sort_keys=True), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
