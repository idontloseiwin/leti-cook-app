"""Small, deterministic pool-pressure diagnostic, not an S/M performance gate.

Uses a fresh disposable PG fixture. Held read transactions intentionally keep
connections occupied until released. This distinguishes pool waiting from SQL,
and checks separate-process-pool topology without claiming collector acceptance.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
import json
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'application'), str(ROOT/'tests')]
from db.base.postgres import Operation, PostgresDatabase
from research_query.admission import ReadAdmission
from research_query_pg import NativeQueryTests, FIXTURE


@contextmanager
def measure_pool(database):
    """Test-only, thread-local observation of real pool acquisition."""
    state = threading.local()
    original = database._pool.connection
    @contextmanager
    def connection(*args, **kwargs):
        began = time.perf_counter()
        with original(*args, **kwargs) as conn:
            state.pool_wait_ms = (time.perf_counter()-began)*1000
            yield conn
    database._pool.connection = connection
    try: yield state
    finally: database._pool.connection = original


def timed_write(database, state, video_id):
    began = time.perf_counter()
    with database.transaction(Operation('research_pressure_write', statement_timeout_ms=1000,
                                         total_timeout_ms=2000, lock_timeout_ms=250)) as tx:
        entered = time.perf_counter()
        tx.execute('UPDATE public.videos SET views=coalesce(views,0)+1 WHERE video_id=%s', (video_id,))
        tx.execute('UPDATE public.videos SET title=title WHERE video_id=%s', (video_id,))
        executed = time.perf_counter()
    ended = time.perf_counter()
    return dict(total_ms=(ended-began)*1000, pool_wait_ms=state.pool_wait_ms,
                setup_ms=(entered-began)*1000-state.pool_wait_ms,
                statements_ms=(executed-entered)*1000,
                commit_release_ms=(ended-executed)*1000)


def probe(reads, writes, *, gated):
    admission = ReadAdmission(concurrency=2) if gated else None
    held, release = threading.Event(), threading.Event()
    lock, active = threading.Lock(), 0
    target = 2 if gated else 4
    def reader():
        nonlocal active
        slot = admission.enter(deadline=time.monotonic()+4, cancelled=lambda: False) if admission else nullcontext()
        with slot:
            with reads.transaction(Operation('research_pressure_read', read_only=True,
                                              statement_timeout_ms=1000, total_timeout_ms=3000)) as tx:
                tx.fetch_one('SELECT 1 AS ok')
                with lock:
                    active += 1
                    if active == target: held.set()
                if not release.wait(2): raise TimeoutError('fixture_release_missing')
    with measure_pool(writes) as state, ThreadPoolExecutor(max_workers=5) as pool:
        readers = [pool.submit(reader) for _ in range(4)]
        try:
            assert held.wait(2), 'readers_not_ready'
            write = pool.submit(timed_write, writes, state, FIXTURE['rows'][0]['id'])
            time.sleep(.25)
            completed_while_held = write.done()
        finally:
            release.set()
        result = write.result(3)
        for future in readers: future.result(3)
    result['completed_while_reads_held'] = completed_while_held
    return result


def run():
    NativeQueryTests.setUpClass()
    database = PostgresDatabase(replace(NativeQueryTests.db.config, min_size=4, max_size=4), admission=lambda _: None).open()
    writer = PostgresDatabase(replace(database.config, min_size=1, max_size=1), admission=lambda _: None).open()
    report = {}
    try:
        # Warm connections/trigger path; don't mistake pool startup for contention.
        with measure_pool(database) as state:
            report['idle'] = timed_write(database, state, FIXTURE['rows'][0]['id'])
        with measure_pool(writer) as state: timed_write(writer, state, FIXTURE['rows'][0]['id'])
        report['ungated_shared_pool'] = probe(database, database, gated=False)
        report['gated_shared_pool'] = probe(database, database, gated=True)
        report['separate_writer_pool'] = probe(database, writer, gated=False)
        assert not report['ungated_shared_pool']['completed_while_reads_held']
        assert report['gated_shared_pool']['completed_while_reads_held']
        assert report['separate_writer_pool']['completed_while_reads_held']
        report.update(completed=True, synthetic_only=True, scale_gate=False)
        Path(sys.argv[1]).write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report, indent=2))
    finally:
        writer.close()
        database.close()
        NativeQueryTests.tearDownClass()


if __name__ == '__main__': run()
