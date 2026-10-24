"""One worker at a time, with cooperative handoff at the next scheduled run."""
from __future__ import annotations
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from pipeline_trace import capture, event, run as trace_run, scope
from self_hosted_settings import browser_environment, DEFAULT_HANDOFF_GRACE_SECONDS, YOUTUBE_REQUEST_CONCURRENCY_ENV
from youtube_errors import KEY_REFUSALS
from runtime.collection_schedules import schedule_environment
from collection_shutdown import (CURSOR_SNAPSHOT_ENV,CURSOR_COMPLETE_PREFIX,CURSOR_MAX_BYTES,
    decode_cursor_checkpoint,encode_cursor_checkpoint)

ROOT = Path(__file__).resolve().parent
MIN_OPERATOR_SLICE_SECONDS = 60
MAX_OPERATOR_SLICE_SECONDS = 1200
TERMINATE_GRACE_SECONDS = 20


# The child reports only an exception class name; its stderr text is never kept.
EXIT_REASON_RE = re.compile(rb'^exit_reason=([A-Za-z_][A-Za-z0-9_]{0,79})\r?\n?$')
STDERR_PARSE_LIMIT = 1 << 20


def read_exit_reason(stream, found):
    """Drain stderr; retain fixed reason and one private bounded control record."""
    parsed,discarding=0,False
    maximum=CURSOR_MAX_BYTES+len(CURSOR_COMPLETE_PREFIX)+2
    try:
        while True:
            line=stream.readline(maximum+1)
            if not line:
                found['cursor_stream_complete']=True
                break
            parsed+=len(line)
            if parsed>STDERR_PARSE_LIMIT:
                found['cursor_invalid']=True;found.pop('cursor_checkpoint',None)
                continue
            if discarding:
                discarding=not line.endswith(b'\n')
                continue
            if len(line)>maximum or not line.endswith(b'\n'):
                if line.startswith(CURSOR_COMPLETE_PREFIX):
                    found['cursor_invalid']=True;found.pop('cursor_checkpoint',None)
                discarding=not line.endswith(b'\n')
                continue
            if line.startswith(CURSOR_COMPLETE_PREFIX):
                if found.get('cursor_seen'):
                    found['cursor_invalid']=True;found.pop('cursor_checkpoint',None)
                found['cursor_seen']=True
                if not found.get('cursor_invalid'):
                    try:
                        value=decode_cursor_checkpoint(line[len(CURSOR_COMPLETE_PREFIX):].rstrip(b'\r\n').decode('ascii'))
                        found['cursor_checkpoint']=encode_cursor_checkpoint(value)
                    except (ValueError,UnicodeError):
                        found['cursor_invalid']=True;found.pop('cursor_checkpoint',None)
                continue
            match=EXIT_REASON_RE.match(line)
            if match:found['reason']='exception:'+match.group(1).decode('ascii')
            elif line.startswith(b'Fatal Python error'):found['reason']='fatal_python_error'
    except (OSError,ValueError):
        found['cursor_invalid']=True;found.pop('cursor_checkpoint',None)
    finally:
        try:stream.close()
        except OSError:pass


def starved_operator_slice(interval_seconds):
    return max(MIN_OPERATOR_SLICE_SECONDS, min(interval_seconds // 3, 300))


class CollectionSupervisor:
    def __init__(self, manager, *, ready, schedules, spawn=subprocess.Popen,
                 clock=time.monotonic, wall_clock=time.time, update_hold_guard=None, provider_snapshot=None):
        self.manager, self.require_ready = manager, ready
        self.update_hold_guard = update_hold_guard
        self._update_operation = None
        self._update_outcome = 'unobserved'
        self._schedule_environment = schedule_environment(schedules)
        self._provider_environment = dict(provider_snapshot or {})
        # This bound belongs to the backend lifetime, not a live config reload.
        if provider_snapshot is not None:
            from provider_runtime import SNAPSHOT_ENV
            captured = self._provider_environment.get(YOUTUBE_REQUEST_CONCURRENCY_ENV)
            if (set(self._provider_environment) != {SNAPSHOT_ENV, YOUTUBE_REQUEST_CONCURRENCY_ENV}
                    or not isinstance(captured, str) or not captured.isascii() or not captured.isdecimal()
                    or not 1 <= int(captured) <= 50 or str(int(captured)) != captured):
                raise ValueError('provider_concurrency_snapshot_invalid')
            self._youtube_request_concurrency = captured
        else:
            self._youtube_request_concurrency = browser_environment(manager.store.read()['settings'])[YOUTUBE_REQUEST_CONCURRENCY_ENV]
        self.spawn, self.clock, self.wall_clock = spawn, clock, wall_clock
        self.stop = threading.Event()
        self.process = self.thread = None
        self.next_auto = self.next_operator = 0
        self.last_wake = None
        self.last_started = {'auto': None, 'operator': None}
        self.deadline = self.started_at = None
        self.handoff_at = self.terminated_at = None
        self.kill_sent = False
        self.finish_grace = DEFAULT_HANDOFF_GRACE_SECONDS
        self.exit_found, self.stderr_reader = {}, None
        self._cursor_checkpoint = None
        self._process_trace = None
        self._trace_gate_state = None
        self.info = {'state': 'waiting', 'worker': None, 'last_exit': None, 'last_exit_reason': None,
                     'running_since': None, 'handoffs': 0, 'timeouts': 0}

    def _trace_gate(self, reason):
        if self._trace_gate_state == reason:
            return
        self._trace_gate_state = reason
        with scope("worker_admission", reason=reason) as trace:
            trace.finish("deferred")

    def start(self):
        self.thread = threading.Thread(target=self._run, name='lic-collection-supervisor', daemon=True)
        self.thread.start()

    def status(self):
        info = dict(self.info)
        if self.started_at is not None:
            info['elapsed_seconds'] = max(0, int(self.clock() - self.started_at))
        return info

    def update_status(self):
        with self.manager.lock:
            return {'observed_operation_id': self._update_operation,
                    'active': self.busy(), 'outcome': self._update_outcome}

    def _observe_update_hold(self, hold):
        operation = hold['operation_id'] if hold['state'] == 'held' else None
        if operation != self._update_operation:
            self._update_operation = operation
            self._update_outcome = ('draining' if self.process is not None else 'idle') if operation else 'unobserved'
            if operation and self.process is not None:
                # A scheduled handoff may already have failed before the hold
                # appeared. Starting a new observation cannot erase that fact.
                if self.terminated_at is not None or self.kill_sent:
                    self._update_outcome = 'forced'
                elif self.info['state'] == 'handoff_unconfirmed':
                    self._update_outcome = 'unconfirmed'

    def _exit_reason(self, code):
        if self.stderr_reader is not None:
            self.stderr_reader.join(timeout=1)
        reason = self.exit_found.get('reason')
        if reason is None and code is not None and code < 0:
            reason = f'signal_{-code}'
        return reason

    def _adopt_cursor(self, code):
        # A COMPLETE record exists only after the child's confirmed final run
        # receipt. Crashes/forced stops/partial streams retain the prior cursor.
        if (self.info['worker']!='auto' or code!=0 or self.terminated_at is not None or self.kill_sent
                or not self.exit_found.get('cursor_stream_complete') or self.exit_found.get('cursor_invalid')
                or self.exit_found.get('reason') or (self.stderr_reader is not None and self.stderr_reader.is_alive())):
            return
        value=self.exit_found.get('cursor_checkpoint')
        if value is not None:
            try:self._cursor_checkpoint=encode_cursor_checkpoint(decode_cursor_checkpoint(value))
            except ValueError:pass

    def busy(self):
        return self.process is not None and self.process.poll() is None

    def _request_handoff(self, now):
        # Queued safely even while our child is importing; never an exception
        # inside provider/DB work. Only this supervisor owns the pipe.
        trace_run(self._process_trace, event, "worker_handoff", "requested", reason="update_hold" if self._update_operation else "scheduled_boundary")
        self.handoff_at = now
        self.info.update(state='finishing_batch', handoffs=self.info['handoffs'] + 1)
        try:
            self.process.stdin.write(b'stop\n')
            self.process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            self.info['state'] = 'handoff_unconfirmed'
            if self._update_operation is not None:
                self._update_outcome = 'unconfirmed'

    def _watch(self, now):
        if self.handoff_at is None:
            if now >= self.deadline:
                self._request_handoff(now)
        elif self.terminated_at is None:
            if now - self.handoff_at >= self.finish_grace:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
                trace_run(self._process_trace, event, "worker_terminate", "requested", reason="handoff_timeout")
                self.terminated_at = now
                self.info.update(state='worker_timeout', timeouts=self.info['timeouts'] + 1)
                if self._update_operation is not None:
                    self._update_outcome = 'forced'
        elif not self.kill_sent and now - self.terminated_at >= TERMINATE_GRACE_SECONDS:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
            trace_run(self._process_trace, event, "worker_kill", "requested", reason="terminate_timeout")
            self.kill_sent = True

    def tick(self):
        with self.manager.lock:
            if self.stop.is_set():
                return
            now = self.clock()
            hold = (self.update_hold_guard.check() if self.update_hold_guard is not None
                    else {'state': 'disabled', 'operation_id': None})
            self._observe_update_hold(hold)
            holding = hold['state'] in ('held', 'invalid')
            if self.process is not None:
                code = self.process.poll()
                if code is None:
                    if self.handoff_at is None:
                        try:
                            if holding:
                                raise RuntimeError('update_hold')
                            self.require_ready()
                        except Exception:
                            # Finish the current owned batch before pausing; keep
                            # the same bounded handoff/termination protocol.
                            self._request_handoff(now)
                    self._watch(now)
                    return
                if self._update_operation is not None and self._update_outcome == 'draining':
                    self._update_outcome = ('forced' if self.terminated_at is not None or self.kill_sent
                                            else 'normal' if code == 0 else 'unconfirmed')
                if self.process.stdin is not None:
                    self.process.stdin.close()
                reason=self._exit_reason(code)
                trace_run(self._process_trace, event, "worker_exit", "completed" if code == 0 else "failed",
                          reason=reason or ("normal_exit" if code == 0 else "nonzero_exit"),
                          duration_ms=max(0, now - self.started_at) * 1000 if self.started_at is not None else None)
                self._process_trace = None
                self._adopt_cursor(code)
                self.info.update(last_exit=code, last_exit_reason=reason, running_since=None,
                                 state='waiting' if code == 0 else 'worker_failed')
                self.process = None
                self.started_at = self.handoff_at = self.terminated_at = None
                self.kill_sent = False
                if self.info['worker'] == 'operator':
                    self.next_operator = now + 60
            if holding:
                self.info['state'] = 'maintenance_unavailable'
                self._trace_gate('maintenance_unavailable')
                return
            value = self.manager.store.read()
            if not value['prepared'] or value['phase'] == 'preparing':
                self.info['state'] = 'waiting_for_setup'
                self._trace_gate('waiting_for_setup')
                return
            if value.get('error') in KEY_REFUSALS:
                # YouTube refused the key: no pass starts until set-key stores one it accepts,
                # then the next tick resumes collection by itself.
                self.info['state'] = 'key_refused'
                self._trace_gate('key_refused')
                return
            settings = value['settings']
            if not settings['automatic_collection']:
                self.info['state'] = 'paused'
                self._trace_gate('paused')
                return
            try:
                self.require_ready()
            except Exception:
                self.info['state'] = 'maintenance_unavailable'
                self._trace_gate('maintenance_unavailable')
                return
            interval = settings['interval_minutes'] * 60
            wake_path = Path(os.environ['OPERATOR_JOB_WAKE_PATH'])
            try:
                wake = wake_path.stat().st_mtime_ns
            except FileNotFoundError:
                wake = None
            if wake != self.last_wake:
                self.last_wake = wake
                self.next_operator = min(self.next_operator, now)
            auto_due, operator_due = now >= self.next_auto, now >= self.next_operator
            last_auto, last_operator = self.last_started['auto'], self.last_started['operator']
            starved = last_auto is not None and (last_operator is None or last_operator < last_auto)
            if auto_due and operator_due and starved:
                kind, budget = 'operator', starved_operator_slice(interval)
            elif auto_due:
                kind, budget = 'auto', interval
            elif operator_due:
                kind = 'operator'
                budget = min(int(self.next_auto - now), MAX_OPERATOR_SLICE_SECONDS)
                if budget < MIN_OPERATOR_SLICE_SECONDS:
                    return
            else:
                return
            if kind == 'operator':
                budget = min(budget, settings['limits']['drain_max_seconds'])
            env = dict(os.environ)
            env.update(browser_environment(settings))
            env.update(self._schedule_environment)
            env.update(self._provider_environment)
            env[YOUTUBE_REQUEST_CONCURRENCY_ENV] = self._youtube_request_concurrency
            env['LIC_COLLECTION_INTERVAL_MINUTES'] = str(settings['interval_minutes'])
            env['LIC_COLLECTION_SUPERVISED'] = '1'
            env.pop(CURSOR_SNAPSHOT_ENV,None)
            if kind=='auto' and self._cursor_checkpoint is not None:
                env[CURSOR_SNAPSHOT_ENV]=self._cursor_checkpoint
            env.pop('LIC_OPERATOR_SLICE_SECONDS', None)
            if kind == 'operator':
                env['LIC_OPERATOR_SLICE_SECONDS'] = str(budget)
            env.pop('YOUTUBE_API_KEY', None)
            self._trace_gate_state = None
            with scope("worker_launch", reason=kind) as trace:
                self._process_trace = capture()
                for field, name in (("trace_id", "LIC_PIPELINE_TRACE_ID"), ("span_id", "LIC_PIPELINE_PARENT_SPAN_ID")):
                    env.pop(name, None)
                    if field in self._process_trace:
                        env[name] = self._process_trace[field]
                process = self.spawn([sys.executable, str(ROOT / 'ops/self_hosted_collect.py'), kind],
                                     cwd=ROOT, env=env, stdin=subprocess.PIPE, bufsize=0,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, close_fds=True)
                trace.finish("spawned")
            self.process = process
            self.exit_found, self.stderr_reader = {}, None
            stream = getattr(process, 'stderr', None)
            if stream is not None:
                self.stderr_reader = threading.Thread(target=read_exit_reason, args=(stream, self.exit_found),
                                                      name='lic-collection-stderr', daemon=True)
                self.stderr_reader.start()
            # Anchor to an actual launch, not a wall-clock slot. A delayed/new
            # backend still gets one full configured interval.
            self.started_at = self.clock()
            self.deadline = self.started_at + budget
            if kind == 'auto':
                self.next_auto = self.deadline
            else:
                self.next_operator = self.started_at + 60
            self.last_started[kind] = self.started_at
            self.finish_grace = settings.get('handoff_grace_seconds', DEFAULT_HANDOFF_GRACE_SECONDS)
            self.info.update(state='running', worker=kind, running_since=self.wall_clock())

    def _run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                with self.manager.lock:
                    self.info['state'] = 'scheduler_unavailable'
                    self._trace_gate('scheduler_unavailable')
            self.stop.wait(3)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
        process = self.process
        if process is not None and process.poll() is None:
            # Backend shutdown retains its separate existing bounded teardown.
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        if process is not None:
            trace_run(self._process_trace, event, "worker_exit", "observed", reason="backend_shutdown")
            self._process_trace = None
        if process is not None and process.stdin is not None:
            process.stdin.close()
