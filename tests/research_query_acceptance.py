"""Manual bounded HTTP acceptance on a freshly created synthetic PG17 cluster.

The caller owns the cluster and wall/disk budget. This is not a production
collector or full application-lifespan test: HOT readiness/sidebar are fixtures.
The owner middleware, HTTP router, query service, restricted DB pool, projection
refresher and retention repository are real. Never loads installation settings.
"""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import argparse
import concurrent.futures
import json
import os
import socket
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
import httpx
import psycopg
import uvicorn
import research_query_benchmark as fixture
from db.base.postgres import Operation, PostgresConfig, PostgresDatabase
from db.hot.hot_retention import HotRetentionRepository
from db.inventory.search_projection import SearchProjectionRepository
from db.reads.research_query import ResearchQueryRepository
from research_projection_refresher import SearchProjectionRefresher
from research_query.validation import QueryRejected
from research_query_preview import create_preview_app
from research_query.service import instant
from research_query_pressure import measure_pool, timed_write

# Write impact. Comparison: one projection refresher runs throughout, as on
# every installation, and blocks of small writes with the same cleanup alternate
# without and with continuous catalogue reads; the arm going first changes every
# round, so a slow period of the server falls on both alike. Diagnostic, apart:
# blocks without and with a refresher, no reads, to see the refresher's own cost.
COMPARISON_ARMS = ('background', 'reads')
DIAGNOSTIC_ARMS = ('refresher_off', 'refresher_on')
WRITE_BLOCK = {'pilot': 5, 'S': 10, 'M': 10}
WRITE_ROUNDS = {'pilot': 2, 'S': 10, 'M': 10}
# The gate compares 'reads' with 'background' on the shared pool. Fewer samples
# per arm neither pass nor fail it.
WRITE_MIN_SAMPLES = 100
READERS = 4
# Every block waits this long before its first write, with or without reads.
BLOCK_LEAD = .2
# Other server processes report their statistics at most once a second; the
# counters after a block are read once they have.
STATS_DELAY = 1.2
_WAL = ('SELECT wal_records,wal_fpi,wal_bytes::float8 AS wal_bytes,wal_buffers_full,wal_write,wal_sync,'
        'wal_write_time,wal_sync_time FROM pg_stat_wal')
_CHECKPOINTER = 'SELECT num_timed,num_requested,write_time,sync_time,buffers_written FROM pg_stat_checkpointer'
_WAITS = ("SELECT backend_type,coalesce(wait_event_type,'CPU'),coalesce(wait_event,'-') FROM pg_stat_activity "
          "WHERE pid<>pg_backend_pid() AND (backend_type<>'client backend' OR state='active')")
_SETTINGS = ('checkpoint_timeout', 'max_wal_size', 'synchronous_commit', 'wal_sync_method',
             'track_wal_io_timing', 'shared_buffers')


_QUEUE = ("SELECT (SELECT count(*) FROM public.video_search_projection_queue) AS queued,"
          "(SELECT count(*) FROM public.video_search_projection WHERE valid_until<=statement_timestamp()) AS expired")


def arm_order(arms, round_number):
    """Every arm once per round; each arm goes first in turn."""
    shift = round_number % len(arms)
    return list(arms[shift:] + arms[:shift])


def queue_size(admin):
    """Queued markers and expired rows waiting for the refresher."""
    queued, expired = admin.execute(_QUEUE).fetchone()
    return {'queued': queued, 'expired': expired}


class CountingRepository:
    """Test-only: counts the refresher's real batches as they happen."""
    def __init__(self, repository):
        self.repository = repository
        self._lock = threading.Lock()
        self._counts = dict(batches=0, markers_taken=0, rows_refreshed=0, failures=0)

    def refresh_counts(self, limit):
        try:
            taken, refreshed = self.repository.refresh_counts(limit)
        except Exception:
            with self._lock:
                self._counts['failures'] += 1
            raise
        with self._lock:
            self._counts['batches'] += 1
            self._counts['markers_taken'] += taken
            self._counts['rows_refreshed'] += refreshed
        return taken, refreshed

    def snapshot(self):
        with self._lock:
            return dict(self._counts)


def server_counters(admin):
    """Cumulative WAL and checkpointer counters. Other processes report theirs
    with a short delay, so a block's difference is approximate."""
    values = {}
    for prefix, statement in (('wal', _WAL), ('checkpointer', _CHECKPOINTER)):
        cursor = admin.execute(statement)
        row = cursor.fetchone()
        values.update({prefix + '.' + column.name: float(value) for column, value in zip(cursor.description, row)})
    return values


def counter_delta(before, after):
    return {key: round(after[key] - before[key], 3) for key in before}


class WaitSampler:
    """Counts server wait events while one block runs: process types and event
    names only, never query text or values. Every fifth poll also notes the
    refresher's queue; the block keeps its largest size. Test-only diagnostic."""
    def __init__(self, connection, interval=.02):
        self.connection, self.interval = connection, interval

    def __enter__(self):
        self.counts, self.polls, self.queue_max = {}, 0, {'queued': 0, 'expired': 0}
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError('wait_sampler_start_timeout')
        if self._error is not None:
            raise self._error
        return self

    def _run(self):
        try:
            # Autocommit: pg_stat_activity is a new snapshot on every poll.
            with psycopg.connect(**self.connection) as conn:
                self._ready.set()
                while not self._stop.is_set():
                    for row in conn.execute(_WAITS):
                        key = ':'.join(row)
                        self.counts[key] = self.counts.get(key, 0) + 1
                    if self.polls % 5 == 0:
                        size = queue_size(conn)
                        self.queue_max = {key: max(self.queue_max[key], size[key]) for key in size}
                    self.polls += 1
                    self._stop.wait(self.interval)
        except Exception as error:
            self._error = error
            self._ready.set()

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(10)
        if self._error is not None and exc[0] is None:
            raise self._error


def settle(admin, budget):
    """A freshly loaded fixture is still vacuuming and checkpointing its load;
    an installation has done that long ago. Start the write blocks after it."""
    begin = time.monotonic()
    admin.execute('ALTER SYSTEM SET track_wal_io_timing = on')
    admin.execute('SELECT pg_reload_conf()')
    admin.execute('VACUUM (ANALYZE)')
    admin.execute('CHECKPOINT')
    vacuumed = time.monotonic()
    while admin.execute("SELECT count(*) FROM pg_stat_activity WHERE backend_type='autovacuum worker'").fetchone()[0]:
        budget()
        time.sleep(.5)
    settings = dict(admin.execute('SELECT name,setting FROM pg_settings WHERE name = ANY(%s)', (list(_SETTINGS),)).fetchall())
    return {'vacuum_checkpoint_seconds': round(vacuumed - begin, 3),
            'autovacuum_wait_seconds': round(time.monotonic() - vacuumed, 3), 'settings': settings}


def write_impact(samples, arms):
    """Per-arm summaries; ratio = median of the second arm / median of the first."""
    rows = {arm: [x for x in samples if x['arm'] == arm] for arm in arms}
    summary = {arm: fixture.summarize([x['write_ms'] for x in values]) for arm, values in rows.items()}
    commit = {arm: fixture.summarize([x['phases']['commit_release_ms'] for x in values]) for arm, values in rows.items()}
    first, second = (summary[arm].get('median_ms') for arm in arms)
    return {'summary': summary, 'commit_release': commit,
            'ratio': round(second / first, 3) if first and second is not None else None}


def stable_answer(value):
    """Compare all returned cells/clocks/provenance and coverage, not page IDs only."""
    return {key: value[key] for key in ('columns', 'rows', 'provenance', 'coverage')} | {
        'has_more': value['page']['has_more']}


def assess(report):
    """Completion, correctness and performance are separate outcomes."""
    warm = fixture.summarize([s['http_ms'] for s in report['samples']
                             if s['case'] == 'folder_long_warm' and s['status'] == 200])
    threshold = report['targets'].get(report['size'] + '_warm_p95_ms')
    writes = report.get('writes', {})
    shared = writes.get('comparison', {}).get('shared', {})
    ratio = shared.get('ratio')
    # Too few samples in either gate arm cannot pass or fail the gate.
    enough = all(shared.get('summary', {}).get(arm, {}).get('n', 0) >= WRITE_MIN_SAMPLES
                 for arm in COMPARISON_ARMS)
    warm_failed = any(s['case'] == 'folder_long_warm' and s['status'] != 200 for s in report['samples'])
    return {'warm_http': warm, 'warm_http_target_ms': threshold,
            'warm_http_pass': False if warm_failed else warm['p95_ms'] <= threshold if threshold and warm.get('p95_ms') is not None else None,
            'write_median_regression_ratio': ratio,
            'write_ratio_pass': ratio <= report['targets']['write_median_regression_ratio'] if ratio is not None and enough else None,
            'refresher_impact_ratio': writes.get('diagnostic', {}).get('ratio'),
            'separate_pool_ratio': writes.get('comparison', {}).get('separate', {}).get('ratio'),
            'refresher_pass': (not writes['refresher_errors'] and not writes.get('refresher_work', {}).get('failures'))
                              if 'refresher_errors' in writes else None,
            'note': 'pilot has no latency acceptance; the write gate needs '
                    f'{WRITE_MIN_SAMPLES} alternated samples per arm; other ratios are diagnostics'}


def verify_generated(value, expected_ids, channels, now):
    assert [row['id'] for row in value['rows']] == expected_ids, 'independent_order_mismatch'
    assert value['context']['valid_before'] == value['source_expires_at']
    for row in value['rows']:
        source = fixture.generated_row(int(row['id'][1:]), channels, now)
        for field, cell in row['cells'].items():
            key = field.removeprefix('video.')
            raw = source[key]
            metadata = key in {'title', 'duration_seconds', 'published_at'}
            expired = source['states'].get('video.title' if metadata else field) == 'expired'
            state = 'expired' if expired else 'unknown' if raw is None else 'known'
            expected = None if state != 'known' else str(raw)
            assert cell['state'] == state, ('cell_state_mismatch', field, row['id'])
            if field == 'video.published_at' and expected:
                assert instant(cell['value']) == instant(expected), 'publication_mismatch'
            else:
                assert cell['value'] == expected, ('cell_value_mismatch', field, row['id'])
            assert cell['provenance_ref'] in value['provenance'], 'provenance_missing'
            if cell['observed_at'] and cell['expires_at']:
                assert instant(cell['expires_at']) >= instant(cell['observed_at'])


@contextmanager
def http_server(database, state, report, projection):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    app = create_preview_app(database, state / 'owner.sqlite3', origin, [fixture.FOLDER],
                             projection=projection, start_projection=False)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                         log_level='error', access_log=False))
    @contextmanager
    def span(operation, phase='component', **kwargs):
        begin = time.perf_counter()
        try:
            yield
        finally:
            report['db_spans'].append({'case': report['phase'], 'operation': operation,
                                      'phase': phase, 'ms': (time.perf_counter() - begin) * 1000})
    with patch.dict(os.environ, LIC_RESEARCH_QUERY_PREVIEW='1'), \
            patch('hot_runtime.require_hot_runtime_ready', lambda **kw: None), \
            patch('ui_read_trace.is_active', lambda: True), patch('ui_read_trace.scope', span):
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if time.monotonic() > deadline:
                    raise RuntimeError('fixture_http_start_timeout')
                time.sleep(.02)
            with httpx.Client(base_url=origin, headers={'Origin': origin}, timeout=12,
                              limits=httpx.Limits(max_connections=16)) as client:
                yield client
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            if thread.is_alive():
                raise RuntimeError('fixture_http_stop_timeout')


def run(args):
    if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE') != '1':
        raise RuntimeError('disposable_cluster_required')
    if not 30 <= args.seconds <= 1100:
        raise RuntimeError('bounded_run_required')
    args.output.mkdir(exist_ok=False)
    fixture.TITLES = args.titles
    started = time.monotonic()
    deadline = started + args.seconds
    report = dict(size=args.size, titles=args.titles, phase='setup', samples=[], db_spans=[], checks={},
                  targets=fixture.TARGETS, limits=dict(pool=4, sql_ms=5000, service_seconds=8),
                  provider_calls=0, production_accessed=False, hot_readiness='fixture',
                  scope='real loopback HTTP; synthetic sources and small writes, not collector acceptance')
    def save():
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        (args.output / 'result.json').write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
    def budget():
        if time.monotonic() > deadline - 15:
            raise TimeoutError('acceptance_budget_exhausted')
    connection = dict(host='127.0.0.1', port=int(os.environ['LIC_QUERY_TEST_PORT']), dbname='leti-cook',
                      user='postgres', password=os.environ['LIC_QUERY_TEST_PASSWORD'], autocommit=True)
    db = writer_db = maintenance_db = None
    try:
        with psycopg.connect(**connection) as admin:
            version = int(admin.execute('SHOW server_version_num').fetchone()[0])
            assert 170000 <= version < 180000, 'pg17_required'
            fixture.bootstrap(admin)  # Refuses an existing application's tables.
            now = fixture.populate(admin, args.size)
            budget()
            report['setup_seconds'] = time.monotonic() - started
            report['postgres'] = version
            report['db_bytes'] = admin.execute('SELECT pg_database_size(current_database())').fetchone()[0]
            report['settings'] = dict(admin.execute("SELECT name,setting FROM pg_settings WHERE name IN ('jit','shared_buffers','work_mem')").fetchall())
            dsn = psycopg.conninfo.make_conninfo(
                host=connection['host'], port=connection['port'], dbname=connection['dbname'],
                user='authenticator', password=connection['password'])
            db = PostgresDatabase(PostgresConfig(dsn, max_size=4), admission=lambda _: None).open()
            writer_db = PostgresDatabase(PostgresConfig(dsn, max_size=4), admission=lambda _: None).open()
            maintenance_db = PostgresDatabase(PostgresConfig(dsn, min_size=1, max_size=1), admission=lambda _: None).open()
            projection = SearchProjectionRepository(db)
            worker = SearchProjectionRefresher(projection, ready=lambda: None)
            channels, videos, _ = fixture.SIZES[args.size]
            queries = fixture.workloads()
            queries['channel_small']['scope']['id'] = 'UC' + f'{min(17, channels - 1):022}'
            expected = {name: fixture.oracle(q, channels, videos, now) for name, q in queries.items()}

            def refresh_all():
                begin = time.perf_counter()
                batches = 0
                while True:
                    budget()
                    changed = projection.refresh()
                    batches += 1
                    if not changed:
                        break
                worker.step()  # Real empty-batch acknowledgement, not a fabricated readiness flag.
                return {'batches': batches, 'ms': (time.perf_counter() - begin) * 1000,
                        'note': 'bounded production refresh transactions, without worker idle pauses'}

            with http_server(db, args.output, report, worker) as client:
                def request(name, q, *, allowed_error=False):
                    budget()
                    begin = time.perf_counter()
                    response = client.post('/research/queries/execute', json=q)
                    received = time.perf_counter()
                    data = response.json()
                    decoded = time.perf_counter()
                    record = {'case': name, 'status': response.status_code, 'http_ms': (received - begin) * 1000,
                              'decode_ms': (decoded - received) * 1000, 'bytes': len(response.content)}
                    if response.status_code != 200:
                        record['error'] = data
                    report['samples'].append(record)
                    if allowed_error and response.status_code != 200:
                        assert data.get('code') == 'catalogue_preparing', ('unexpected_cold_error', data)
                        assert response.status_code == 503 and response.headers['retry-after'] == '2'
                    else:
                        assert response.status_code == 200, ('http_request_failed', name, response.status_code, data)
                    return data if response.status_code == 200 else None

                report['phase'] = 'unfilled_projection'
                cold = request('unfilled_projection', queries['folder_long'], allowed_error=True)
                if cold:
                    verify_generated(cold, expected['folder_long'], channels, now)
                save()
                report['phase'] = 'fill_projection'
                report['projection_fill'] = refresh_all()
                admin.execute('ANALYZE public.video_search_projection')
                references = {}
                for name, q in queries.items():
                    report['phase'] = name
                    data = request(name, q)
                    verify_generated(data, expected[name], channels, now)
                    references[name] = stable_answer(data)
                    repeats = 1 if args.size == 'pilot' else 20 if name == 'folder_long' else 3
                    for _ in range(repeats):
                        assert stable_answer(request(name + '_warm', q)) == references[name], 'warm_answer_changed'
                    save()
                if cold:
                    assert stable_answer(cold) == references['folder_long'], 'cold_warm_mismatch'

                # Keyset continuations for every type, verified against independent offsets.
                report['phase'] = 'pages'
                for kind in ('long', 'short', 'stream'):
                    q = deepcopy(queries['folder_' + kind])
                    offset = 0
                    for page in range(3 if args.size == 'pilot' else 8):
                        data = request('page_' + kind, q)
                        verify_generated(data, fixture.oracle(q, channels, videos, now, offset), channels, now)
                        offset += len(data['rows'])
                        if not data['page']['has_more']:
                            break
                        q['page']['after'] = data['page']['next_cursor']
                report['checks']['all_type_pages'] = True
                save()

                report['phase'] = 'concurrent'
                for workers in (4, 8):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                        for wave in range(1 if args.size == 'pilot' else 3):
                            results = list(pool.map(lambda _: request('concurrent_' + str(workers), queries['folder_long']), range(workers)))
                            assert all(stable_answer(v) == references['folder_long'] for v in results), 'concurrent_answer_changed'

                # Small writes to a short's source while long-form reads run; real
                # triggers, refresher and cleanup. Counter and membership clocks are
                # not fabricated. Production has distinct collector and maintenance
                # pools: the shared topology is the stress case and holds the gate.
                report['phase'] = 'write_settle'
                settled = settle(admin, budget)
                topologies = {'shared': (db, db), 'separate': (writer_db, maintenance_db)}
                cursors = dict.fromkeys(topologies)
                write_samples, blocks, refresher_errors = [], [], []
                measured_from = time.monotonic()

                def write_probe(count, topology, labels):
                    write_database, cleanup_database = topologies[topology]
                    retention = HotRetentionRepository(cleanup_database)
                    with measure_pool(write_database) as timings:
                        for _ in range(count):
                            budget()
                            at = time.monotonic() - measured_from
                            measured = timed_write(write_database, timings, 'z0000000010')
                            cleanup = retention.cleanup_page('video_snapshots', after_id=cursors[topology], limit=20)
                            cursors[topology] = cleanup.next_cursor
                            write_samples.append({**labels, 't_s': round(at, 3), 'write_ms': measured['total_ms'],
                                                  'phases': measured, 'cleanup_deleted': cleanup.deleted})
                            time.sleep(.05)

                def stop_refresher(owner):
                    owner.close()
                    if owner.last_error is not None:
                        refresher_errors.append(owner.last_error)

                def write_block(phase, topology, arm, round_number, counting):
                    # Same writes, cleanup and lead in every arm; only reads (comparison)
                    # or the refresher (diagnostic) differ.
                    labels = {'phase': phase, 'topology': topology, 'arm': arm, 'round': round_number}
                    own = (SearchProjectionRefresher(counting, ready=lambda: None)
                           if arm == 'refresher_on' else None)
                    stop_reads = threading.Event()
                    def reader():
                        while not stop_reads.is_set():
                            data = request('write_impact_reads', queries['folder_long'])
                            assert stable_answer(data) == references['folder_long'], 'write_read_answer_changed'
                    before, work_before = server_counters(admin), counting.snapshot()
                    queue_before = queue_size(admin)
                    started_at = time.monotonic() - measured_from
                    with WaitSampler(connection) as waits, \
                            concurrent.futures.ThreadPoolExecutor(max_workers=READERS) as pool:
                        readers = []
                        if own is not None:
                            own.start()
                        try:
                            if arm == 'reads':
                                readers = [pool.submit(reader) for _ in range(READERS)]
                            time.sleep(BLOCK_LEAD)
                            write_probe(WRITE_BLOCK[args.size], topology, labels)
                        finally:
                            stop_reads.set()
                            for future in readers:
                                future.result()  # Waits for reads in flight; raises their failures.
                            if own is not None:
                                stop_refresher(own)
                    ended_at = time.monotonic() - measured_from
                    queue_after, work_after = queue_size(admin), counting.snapshot()
                    time.sleep(STATS_DELAY)
                    blocks.append({**labels, 'started_s': round(started_at, 3), 'ended_s': round(ended_at, 3),
                                   'refresher': counter_delta(work_before, work_after),
                                   'queue': {'start': queue_before, 'end': queue_after, 'max_sampled': waits.queue_max},
                                   'server': counter_delta(before, server_counters(admin)),
                                   'wait_polls': waits.polls, 'waits': waits.counts})

                # Comparison: one refresher for all of it, never restarted between blocks.
                counting = CountingRepository(projection)
                continuous = SearchProjectionRefresher(counting, ready=lambda: None)
                continuous.start()
                try:
                    for topology in topologies:
                        for round_number in range(WRITE_ROUNDS[args.size]):
                            for arm in arm_order(COMPARISON_ARMS, round_number):
                                report['phase'] = f'writes_{topology}_{arm}'
                                write_block('comparison', topology, arm, round_number, counting)
                        save()
                finally:
                    stop_refresher(continuous)
                # Diagnostic: no reads; a refresher only in 'refresher_on' blocks,
                # started at the block's start (an upper bound of its cost).
                for round_number in range(WRITE_ROUNDS[args.size]):
                    for arm in arm_order(DIAGNOSTIC_ARMS, round_number):
                        report['phase'] = f'writes_diagnostic_{arm}'
                        write_block('diagnostic', 'shared', arm, round_number, counting)
                comparison = [x for x in write_samples if x['phase'] == 'comparison']
                report['writes'] = {
                    'method': {'comparison_arms': COMPARISON_ARMS, 'diagnostic_arms': DIAGNOSTIC_ARMS,
                               'block': WRITE_BLOCK[args.size], 'rounds': WRITE_ROUNDS[args.size],
                               'readers': READERS, 'lead_seconds': BLOCK_LEAD, 'min_samples_per_arm': WRITE_MIN_SAMPLES,
                               'gate': 'comparison, shared pool: median of reads / median of background',
                               'note': 'one refresher runs through the whole comparison; the diagnostic '
                                       'compares blocks without and with a refresher, no reads; '
                                       'small synthetic writes, not collector acceptance'},
                    'settle': settled,
                    'comparison': {topology: write_impact([x for x in comparison if x['topology'] == topology],
                                                          COMPARISON_ARMS) for topology in topologies},
                    'diagnostic': write_impact([x for x in write_samples if x['phase'] == 'diagnostic'],
                                               DIAGNOSTIC_ARMS),
                    'blocks': blocks, 'samples': write_samples, 'refresher_errors': refresher_errors,
                    'refresher_work': counting.snapshot()}
                report['checks']['concurrent_answers'] = True
                save()

                report['phase'] = 'timeout_recovery'
                repository = ResearchQueryRepository(db)
                for _ in range(4):  # More than one connection must remain reusable.
                    try:
                        repository._read('SELECT pg_sleep(1)', {}, deadline=time.monotonic() + .03,
                                         cancelled=lambda: False, name='acceptance_timeout')
                    except QueryRejected as error:
                        assert error.code == 'query_budget_exceeded'
                    else:
                        raise AssertionError('timeout_not_enforced')
                assert stable_answer(request('after_timeout', queries['folder_long'])) == references['folder_long']
                report['checks']['pool_recovers_after_timeout'] = True

                # Queued source must be visible before the worker refreshes it.
                report['phase'] = 'queued_source'
                one = deepcopy(queries['folder_short'])
                one['where'] = {'field': 'video.id', 'op': 'eq', 'value': 'z0000000010'}
                admin.execute("UPDATE public.videos SET title='Acceptance changed title' WHERE video_id='z0000000010'")
                queued = request('queued_source', one)
                assert queued['rows'][0]['cells']['video.title']['value'] == 'Acceptance changed title'
                refresh_all()
                assert stable_answer(request('refreshed_source', one)) == stable_answer(queued)
                report['checks']['queued_source_visible_before_refresh'] = True
                report['completed'] = True
    except Exception as error:
        report['failure'] = {'type': type(error).__name__, 'stage': report['phase']}
        raise
    finally:
        if maintenance_db is not None: maintenance_db.close()
        if writer_db is not None: writer_db.close()
        if db is not None:
            db.close()
        report['summaries'] = {name: fixture.summarize([s['http_ms'] for s in report['samples']
                              if s['case'] == name and s['status'] == 200])
                              for name in sorted({s['case'] for s in report['samples']})}
        report['assessment'] = assess(report)
        save()
    print(json.dumps({'completed': report.get('completed'), 'elapsed': report['elapsed_seconds'],
                      'checks': report['checks'], 'output': str(args.output)}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--size', choices=fixture.SIZES, required=True)
    parser.add_argument('--titles', choices=fixture.TITLE_MODES, default='cyrillic')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=180)
    run(parser.parse_args())
