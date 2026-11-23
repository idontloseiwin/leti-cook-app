"""A private telemetry journal for test classes that replace os.environ.

Replacing the environment also drops TELEMETRY_PATH. Telemetry emitted inside
it then goes to the default journal: the shared temporary folder, or on
Windows the current folder when the temporary folder was not resolved yet.
Such a class adds this journal to its replacement environment instead.
"""
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch


def private_journal(case_class):
    """Return {'TELEMETRY_PATH': ...} in a new folder removed after the class."""
    folder = tempfile.mkdtemp(prefix='leti-cook-test-telemetry-')
    environment = {'TELEMETRY_PATH': str(Path(folder) / 'telemetry.jsonl')}

    def remove():
        import telemetry_sink
        # Stop this journal's writer first, so a late batch cannot recreate the folder.
        with patch.dict(os.environ, environment, clear=True):
            telemetry_sink.default_emitter().close(2.0)
        shutil.rmtree(folder, ignore_errors=True)

    case_class.addClassCleanup(remove)
    return environment


def telemetry_switched_on(case):
    """Turn the switched-off pipeline telemetry on for one test.

    The product keeps telemetry off until it is redesigned. Tests of the old
    emitter path, routes and traces, which stay until the redesign replaces
    them, enable it explicitly here.
    """
    import telemetry_sink
    switch = patch.object(telemetry_sink, 'PIPELINE_TELEMETRY_OFF', False)
    switch.start()
    case.addCleanup(switch.stop)
