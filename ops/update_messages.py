"""Fixed causes of a failed update and what the owner can do about each.

The engine records one of these codes as the journal's failure_code and prints
it with its hint. Exception text is never recorded or printed: it can carry
credentials or source data.
"""
from __future__ import annotations

import re

# The journal accepts exactly this shape for failure_code.
CODE = re.compile(r'[a-z][a-z0-9_]{0,95}')
# A failed host command carries no reason of its own; its step names the cause.
COMMAND_FAILED = re.compile(r'command_failed_[0-9]{1,3}')
DISK_FULL = 'update_insufficient_disk_space'
STOPPED_BY_USER = 'update_stopped_by_user'
UNEXPECTED = 'update_unexpected_error'

# Journal phase current when a host command or file operation failed without
# a fixed code. Phases that only save the journal have no step of their own.
STEP_CAUSES = {
    'preparing': 'update_preparation_failed',
    'hold_requested': 'update_collection_pause_failed',
    'backend_stop_intent': 'update_backend_stop_failed',
    'db_stop_intent': 'update_db_stop_failed',
    'copy_intent': 'update_copy_failed',
    'swap_intent': 'update_file_swap_failed',
    'swapped': 'update_file_swap_failed',
    'schema_intent': 'update_schema_step_failed',
    'new_start_intent': 'update_backend_start_failed',
    'new_started': 'update_runtime_health_failed',
}

_COLLECTION = 'Collection did not stop in time. Run the update again when no collection pass is running.'
_APPLICATION_STOP = ('The application did not stop cleanly. Run the update again; '
                     'if it repeats, keep this code and request help.')
_DATABASE_STOP = ('The database did not stop cleanly. Run the update again; '
                  'if it repeats, keep this code and request help.')
_SCHEMA = ("The new version's database step did not finish. "
           'Keep this code and both versions and request help before retrying.')
# Hints say what to do, never whether recovery worked: the engine says that.
HINTS = {
    DISK_FULL: ("A disk is too full for the update and its recovery copy. Free space on the disks that hold "
                "the installation folder and Docker's data, then run the update again. "
                'update --check does not measure this space.'),
    'update_preparation_failed': ('Images or files of the new version could not be prepared, for example a '
                                  'download or build failed. Check network access and free disk space, '
                                  'then run the update again.'),
    'update_collection_stop_unconfirmed': _COLLECTION,
    'update_collection_stop_timeout': _COLLECTION,
    'update_collection_pause_failed': ('The application did not answer while collection was being paused. '
                                       'Check that it responds, then run the update again.'),
    'update_backend_stop_not_clean': _APPLICATION_STOP,
    'update_backend_stop_failed': _APPLICATION_STOP,
    'update_db_stop_not_clean': _DATABASE_STOP,
    'update_db_stop_failed': _DATABASE_STOP,
    'update_postgres_not_cleanly_stopped': _DATABASE_STOP,
    'update_copy_failed': 'The recovery copy could not be made. Check free disk space, then run the update again.',
    'update_file_swap_failed': ('The installation could not be switched to the new version. '
                                'Keep this code and request help before retrying.'),
    'update_schema_not_completed': _SCHEMA,
    'update_schema_step_failed': _SCHEMA,
    'update_backend_start_failed': ("The new version did not start. Often another program took the "
                                    "installation's port: free it, then run the update again."),
    'update_runtime_health_failed': ('The new version did not pass its health check in time. '
                                     'Keep this code and both versions and request help before retrying.'),
    STOPPED_BY_USER: 'The update was stopped from the terminal. Run it again when ready.',
    UNEXPECTED: 'Keep this code and the installed version and request help.',
}


def cause_line(code):
    return 'Cause: ' + code + '.' + (' ' + HINTS[code] if code in HINTS else '')
