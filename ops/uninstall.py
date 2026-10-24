"""Confirmed scoped removal. A captured recovery helper survives removal of I."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ops.update_copy_store import load_layout
from ops.update_journal import update_lock, load_journal, TERMINAL
from ops.uninstall_resources import DockerResources
from ops import uninstall_store as store
from ops import update_copy_store as copies


def inspect_copy(layout, expected_operation):
    """Validate the finite timer identity without showing or changing private data."""
    paths = (layout.key_path, layout.cipher_path, layout.partial_path, layout.marker_path,
             layout.owner / copies.OPERATION_FILENAME)
    if not any(os.path.lexists(path) for path in paths):
        return None
    reservation = copies._reservation(layout)
    if reservation.copy_id != expected_operation:
        raise ValueError('uninstall_copy_operation_mismatch')
    for name, data in copies._unit_data(reservation).items():
        path = layout.systemd_directory / name
        if os.path.lexists(path) and copies._read(path, mode=0o644) != data:
            raise ValueError('uninstall_copy_unit_changed')
    return reservation


def cancel_copy(operation):
    layout = operation.layout
    reservation = inspect_copy(layout, operation.inventory['update_operation_id'])
    if reservation is None:
        return
    service = copies._unit_name(reservation.copy_id) + '.service'
    try:
        if (layout.systemd_directory / service).exists():
            # Only the already validated purge service, never an arbitrary unit.
            copies._systemctl(['stop', service])
        copies.purge_copy(layout, expected_copy_id=reservation.copy_id, force=True)
    except (OSError, subprocess.SubprocessError):
        raise ValueError('uninstall_copy_cleanup_failed') from None


def _run(operation, *, emit=print):
    """Every group is idempotent and checks its captured resource identities."""
    inventory = operation.inventory
    if operation.phase == 'preparing':
        operation = store.prepare_uninstall(operation.layout, inventory)
    docker = DockerResources(operation.layout.project, operation.layout.installation)
    phases = ('prepared', 'stop_intent', 'workers_stopped', 'copy_intent', 'copy_removed',
              'resources_intent', 'resources_removed', 'trees_intent', 'trees_removed', 'scope_intent')
    def before(phase):
        return phases.index(operation.phase) < phases.index(phase)
    if before('workers_stopped'):
        operation = store.advance(operation, 'stop_intent')
        docker.stop(inventory['docker'])
        operation = store.advance(operation, 'workers_stopped')
    if before('copy_removed'):
        operation = store.advance(operation, 'copy_intent')
        cancel_copy(operation)
        operation = store.advance(operation, 'copy_removed')
    if before('resources_removed'):
        operation = store.advance(operation, 'resources_intent')
        for method in (docker.remove_containers, docker.remove_volumes, docker.remove_networks, docker.remove_images):
            result = method(inventory['docker'])
            for field in ('removed', 'absent', 'retained'):
                for name in result.get(field, ()):
                    if isinstance(name, dict):
                        emit(field + ': ' + name['id'] + ' (' + name['reason'] + ')')
                    else:
                        emit(field + ': ' + name)
        docker.verify_removed(inventory['docker'])
        operation = store.advance(operation, 'resources_removed')
    if before('trees_removed'):
        operation = store.remove_installation_trees(operation)
    store.finalize_uninstall(operation)
    emit('Installation removed. Shared images were kept where needed.')
    return 0


def uninstall(directory, state, *, ask=input, emit=print):
    from ops.update_engine import UpdateEngine
    from ops.update_host import Host
    layout = load_layout(directory, project=state.get('project'), database_id=state.get('database_id'))
    with update_lock(layout):
        existing = store.load_uninstall(layout)
        if existing is not None:
            emit('Continuing the removal you already confirmed.')
            return _run(existing, emit=emit)
        pending = load_journal(layout)
        if pending and pending['phase'] not in TERMINAL:
            try:
                UpdateEngine(Host(layout), pending, emit=emit).recover()
            except (ValueError, OSError, RuntimeError):
                emit('Update recovery could not finish. Confirming removal also destroys its remaining recovery copy.')
                pending = load_journal(layout)
            else:
                emit('Update recovery completed. Run uninstall again from the recovered installation.')
                return 0
        docker = DockerResources(layout.project, layout.installation)
        captured = docker.inspect_plan(pending)
        inventory = {'docker': captured, 'port': state['port'],
                     'update_operation_id': pending['operation_id'] if pending else None}
        store.inspect_trees(layout)
        inspect_copy(layout, inventory['update_operation_id'])
        emit('Before removing it, open http://127.0.0.1:' + str(state['port']) + '/app-explorer-v3')
        emit('Use Backup and restore → Download backup to keep your work.')
        emit('Installation: ' + str(directory))
        emit('Project: ' + layout.project)
        emit('Its database, sign-in state, update copies and installation files will be permanently removed.')
        for name in ('containers', 'volumes', 'networks', 'images'):
            emit(name + ': ' + str(len(captured[name])))
        if ask('Type ' + layout.project + ' to permanently remove this installation: ') != layout.project:
            emit('Nothing was removed.')
            return 2
        operation = store.prepare_uninstall(layout, inventory)
        emit('If interrupted, resume with: sudo python3 -I -B ' + str(operation.helper_path))
        return _run(operation, emit=emit)


def resume(helper_root, *, emit=print):
    try:
        operation = store.resume_scope(Path(helper_root))
        with store.removal_lock(operation):
            return _run(operation, emit=emit)
    except (OSError, ValueError, RuntimeError, KeyboardInterrupt):
        emit('Removal did not finish. Its saved helper can resume after the reported resource problem is fixed.')
        return 1


def continue_removal(directory, state, *, emit=print):
    """Do not let an ordinary management command revive a confirmed removal."""
    # This read-only existence check keeps source/version commands independent
    # of a root-only installation store when no removal was ever started.
    root = Path(str(directory) + '.update')
    if not (root / 'owner' / store.JOURNAL_FILENAME).exists():
        return False
    layout = load_layout(directory, project=state.get('project'), database_id=state.get('database_id'))
    with update_lock(layout):
        operation = store.load_uninstall(layout)
        if operation is None:
            return False
        emit('Continuing the removal you already confirmed.')
        _run(operation, emit=emit)
        return True
