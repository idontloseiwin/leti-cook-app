"""Public management integration for verified online/offline updates and recovery."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import secrets
import time

from application.release_version import parse_version, read_version
from ops.release_manifest import load_source
from ops.release_download import fetch_release
from ops.release_verification import VerifiedRelease, verify_release
from ops.update_copy_store import load_layout
from ops.update_engine import UpdateEngine, UpdateError
from ops.update_host import Host
from ops import update_journal as journal


def layout_for(directory, state):
    return load_layout(directory, project=state.get('project'), database_id=state.get('database_id'))


@contextmanager
def management(directory, state):
    """Serialize management, then recover before executing any requested action."""
    layout = layout_for(directory, state)
    with journal.update_lock(layout):
        pending = journal.load_journal(layout)
        recovered = False
        if pending and pending['phase'] not in journal.TERMINAL:
            print('An interrupted update was found. Recovering it first.')
            recovered = UpdateEngine(Host(layout), pending).recover()
        yield layout, recovered


def update(layout, package, *, check=False, pre=False, build_local=False, emit=print):
    root = layout.installation
    source = load_source(root / 'application/release-source.json')
    key = root / 'application/release-signing.pub'
    release = (fetch_release(source=source, public_key_path=key, include_prereleases=pre)
               if package is None else verify_release(package, public_key_path=key, source=source))
    return update_verified(layout, release, check=check, pre=pre, build_local=build_local, emit=emit)


def update_verified(layout, release, *, check=False, pre=False, build_local=False,
                    reader_source_root=None, emit=print):
    """Trusted captured-release entry; no package reread or alternate trust root."""
    if type(release) is not VerifiedRelease:
        raise UpdateError('update_verified_release_required')
    root = layout.installation
    current = read_version(root / 'application/VERSION')
    target = release.manifest['version']
    if parse_version(target) == parse_version(current):
        emit('Already using this release: ' + current + '.')
        return 0
    from ops.update_candidate import preflight_candidate
    plan = preflight_candidate(root, release, allow_prerelease=pre)
    emit('Installed: ' + current + '. Available: ' + target + '.')
    reader = {} if reader_source_root is None else {'reader_source_root': reader_source_root}
    host = Host(layout, plan=plan, release=release, build_local=build_local, **reader)
    if check:
        host.check_readiness()
        emit('The package is verified. Read-only installation checks passed. No update was started.')
        emit('Image preparation, copy space and atomic file replacement are checked when applying the update.')
        return 0
    observed = host.preflight()
    value = {'format': 'leti-cook-update', 'version': 1, 'operation_id': secrets.token_hex(16),
             'project': layout.project, 'database_id': layout.database_id, 'phase': 'preparing',
             'old_version': current, 'new_version': target,
             'old_tree': journal.tree_identity(root), 'new_tree': None,
             'old_images': observed['old_images'], 'new_images': None,
             'old_volume': observed['old_volume'], 'restore_volume': None,
             'runtime_epoch': observed['runtime_epoch'], 'started_at': int(time.time()),
             'stopped_at': None, 'copy': None, 'failure_code': None}
    emit('Update journal: ' + str(layout.owner / journal.JOURNAL_FILENAME))
    UpdateEngine(host, value, emit=emit).update()
    return 0


def restore_update_copy(layout, *, ask=input, emit=print):
    value = journal.load_journal(layout)
    if value is None or value['phase'] in journal.TERMINAL or value['copy'] is None:
        raise UpdateError('update_restore_unavailable')
    host = Host(layout)
    host.check_restore(value)
    emit('This restores the database and version from the interrupted update.')
    if ask('Type the project name ' + layout.project + ' to restore: ').strip() != layout.project:
        emit('Nothing was changed.')
        return 2
    engine = UpdateEngine(host, value, emit=emit)
    engine.phase('rollback_intent')
    engine.recover()
    return 0
