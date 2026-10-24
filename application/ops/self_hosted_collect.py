"""Fixed child entrypoint for the fresh-install collection supervisor."""
import os
import re
from contextlib import nullcontext
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))


def operator_arguments(interval_minutes, environ):
    """Drain arguments; a supervisor-granted slice replaces the worker's clock-based window."""
    arguments = ['--drain', '--auto-sync-interval-minutes', str(interval_minutes)]
    granted = environ.get('LIC_OPERATOR_SLICE_SECONDS', '')
    if granted.isdigit() and 1 <= int(granted) <= 1200:
        configured = environ.get('OPERATOR_JOB_DRAIN_MAX_SECONDS', '')
        limit = min(int(granted), int(configured)) if configured.isdigit() and int(configured) > 0 else int(granted)
        arguments += ['--supervised', '--max-seconds', str(limit)]
    return arguments


def _main():
    if os.getenv('LIC_DEPLOYMENT_PROFILE') != 'self-hosted' or sys.argv[1:] not in (['auto'], ['operator']):
        return 2
    supervised = os.getenv('LIC_COLLECTION_SUPERVISED') == '1'
    from collection_shutdown import request_handoff, start_control_reader
    from hot_runtime import supervised_collection_handoff
    if supervised:
        start_control_reader(sys.stdin.buffer)
    with supervised_collection_handoff(request_handoff) if supervised else nullcontext():
        return _collect()


def main():
    from pipeline_trace import bind, scope
    identity = {}
    for field, name in (("trace_id", "LIC_PIPELINE_TRACE_ID"), ("span_id", "LIC_PIPELINE_PARENT_SPAN_ID")):
        value = os.environ.get(name)
        if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value):
            identity[field] = value
    # Invalid observation IDs never become a collection admission condition.
    with bind(**identity), scope("worker_child", reason=sys.argv[1] if sys.argv[1:] in (["auto"], ["operator"]) else "invalid_arguments") as trace:
        code = _main()
        trace.finish("completed" if code == 0 else "failed")
        return code


def _collect():
    from pipeline_trace import event
    event("worker_admission", "checking", phase="configuration")
    from self_hosted_configuration import ConfigurationStore, CONFIG_ENV
    from self_hosted_settings import worker_environment
    value = ConfigurationStore(os.environ[CONFIG_ENV]).read()
    if not value['prepared'] or not value['settings']['automatic_collection']:
        from pipeline_trace import event
        event('worker_admission', 'deferred', reason='setup_or_collection_disabled')
        return 0
    os.environ.update(worker_environment(value['settings'], os.environ))
    # Validate the parent's snapshot before admission or importing the collector.
    # core owns this process's immutable model; subsequent imports reuse it.
    event("worker_admission", "checking", phase="runtime_initialization")
    import core
    from runtime.collection_schedules import require_schedule_model
    require_schedule_model(core.COLLECTION_SCHEDULES)
    from hot_runtime import require_hot_runtime_ready
    event('worker_admission', 'checking', phase='maintenance')
    require_hot_runtime_ready(operation='collection')
    event('worker_admission', 'ready', phase='maintenance')
    from collection_shutdown import handoff_requested
    if handoff_requested():
        from pipeline_trace import event
        event("worker_admission", "deferred", reason="handoff_requested")
        return 0
    from provider_runtime import start_worker, stop_runtime, request_scope
    provider = start_worker(os.environ)
    try:
        with request_scope(sys.argv[1]):
            return _run_collection(value)
    finally:
        stop_runtime(provider)


def _run_collection(value):
    from scripts.run_operator_job_worker import acquire_shared_sync_lock, cli
    if sys.argv[1] == 'operator':
        return cli(operator_arguments(value['settings']['interval_minutes'], os.environ))
    handle = acquire_shared_sync_lock()
    if handle is None:
        from pipeline_trace import event
        event("worker_admission", "deferred", reason="shared_lock_busy")
        return 0
    try:
        import auto_sync
        sys.argv = ['auto_sync.py']
        return auto_sync.main()
    finally:
        handle.close()


if __name__ == '__main__':
    try: code = main()
    except Exception as error:
        # The supervisor keeps only this class name, never the message.
        sys.stderr.write(f'exit_reason={type(error).__name__}\n')
        sys.stderr.flush()
        code = 1
    raise SystemExit(code)
