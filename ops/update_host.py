"""Local Docker effects for the durable updater. Never prints command output."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import secrets
import time

from install import (command, compose_prefix, check_docker, check_time_sync,
                     validate_composed, write_private, InstallError)
from application.update_copy_protocol import Marker, UpdateCopyError, WINDOW_READ_SECONDS, marker_from_window
from application.release_version import parse_version
from ops.update_copy_store import (arm_copy, publish_copy, purge_copy, validate_restore)
from ops.update_engine import UpdateError, EARLY
from ops.release_images import ensure_release_image
from ops import update_journal as journal
from ops.schema_upgrade import PSQL_ARGUMENTS, run_schema_upgrade, reconcile_schema_upgrade

IMAGE = re.compile(r'sha256:[0-9a-f]{64}')
CONTAINER = re.compile(r'[0-9a-f]{64}')
LABEL = 'org.leti-cook.update-operation'
PROJECT_LABEL = 'com.docker.compose.project'
HELPER_LIMITS = ['--memory=256m', '--cpus=1', '--pids-limit=128', '--log-driver=none']
SPACE_MARGIN = 2 * 1024 ** 3
MAX_COPY = 1024 ** 4
MAX_ENTRIES = 1000000


def _window_read(read):
    # The read prints only a fixed code on failure; captured output is never
    # surfaced. Name the refusal instead of a generic container failure.
    try:
        return read()
    except InstallError:
        raise UpdateCopyError('update_copy_window_unavailable') from None


def require(value, code):
    if not value:
        raise UpdateError(code)


def runtime_override(images, volume):
    require(set(images) == {'db', 'backend'} and all(IMAGE.fullmatch(v) for v in images.values()),
            'update_image_identity_invalid')
    return {'services': {name: {'image': images[name]} for name in ('db', 'backend')},
            'volumes': {'database-data': {'name': volume}}}


def healthy_admission(status, *, held=False, startup=False):
    permitted = {'ready', 'collection_priority'}
    if held:
        permitted.add('update_hold')
    if startup:
        permitted.add('startup_cleanup')
    require(status['hot']['user_work'] == {'allowed': True, 'reason': 'ready'}
            and all(item['reason'] in permitted for item in status['hot'].values()),
            'update_runtime_admission_unsafe')


def write_override(root, images, volume):
    data = json.dumps(runtime_override(images, volume), sort_keys=True).encode() + b'\n'
    path = root / 'runtime.compose.json'
    temporary = root / 'runtime.compose.json.pending'
    # A previous interrupted atomic write is ours only in this protected root.
    if temporary.exists():
        info = temporary.lstat()
        require(not temporary.is_symlink() and info.st_uid == 0 and info.st_nlink == 1,
                'update_runtime_file_untrusted')
        temporary.unlink()
    write_private(temporary, data)
    os.replace(temporary, path)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Host:
    def __init__(self, layout, *, plan=None, release=None, build_local=False, reader_source_root=None,
                 run=command, clock=time.time, pause=time.sleep):
        self.layout, self.plan, self.release = layout, plan, release
        self.build_local, self.run, self.clock, self.pause = build_local, run, clock, pause
        self.root = layout.installation
        self.project = layout.project
        self.old_images = None
        self.sizes = None
        self.reader_source_root = reader_source_root
        if reader_source_root is not None:
            from ops.update_reader import validate_reader_source
            validate_reader_source(reader_source_root, release, plan)

    def docker(self, args, *, timeout=300, input_data=None):
        return self.run(['docker'] + args, timeout=timeout, input_data=input_data)

    def compose(self, args, *, timeout=300, input_data=None):
        return self.run(compose_prefix(self.root, self.project) + args, cwd=self.root,
                        timeout=timeout, input_data=input_data)

    def inspect(self, kind, name):
        values = json.loads(self.docker([kind, 'inspect', name]))
        require(type(values) is list and len(values) == 1, 'update_resource_missing')
        return values[0]

    def containers(self):
        ids = self.docker(['ps', '-aq', '--no-trunc', '--filter', 'label=' + PROJECT_LABEL + '=' + self.project]).split()
        result = {}
        for ident in ids:
            require(CONTAINER.fullmatch(ident), 'update_container_identity_invalid')
            value = self.inspect('container', ident)
            labels = value.get('Config', {}).get('Labels', {})
            name = labels.get('com.docker.compose.service')
            require(labels.get(PROJECT_LABEL) == self.project and name in {'db', 'backend'}
                    and name not in result and labels.get('com.docker.compose.oneoff', '').lower() == 'false',
                    'update_unexpected_project_container')
            require(labels.get('com.docker.compose.project.working_dir') in
                    {str(self.root), str(self.root / 'database')}, 'update_container_path_mismatch')
            result[name] = value
        return result

    def volume(self, name):
        require(type(name) is str and name.startswith(self.project + '_')
                and re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}', name), 'update_volume_identity_invalid')
        value = self.inspect('volume', name)
        labels = value.get('Labels') or {}
        require(labels.get(PROJECT_LABEL) == self.project
                and labels.get('com.docker.compose.volume') == 'database-data'
                and value.get('Driver') == 'local' and not value.get('Options'),
                'update_volume_not_owned')
        return value

    def volume_idle(self, name):
        self.volume(name)
        ids = self.docker(['ps', '-q', '--no-trunc', '--filter', 'volume=' + name]).split()
        require(not ids, 'update_volume_has_running_writer')

    def status(self, operation=None):
        args = ['exec', '-T', 'backend', 'python', 'ops/update_runtime.py', 'status']
        if operation:
            args += ['--operation', operation]
        return json.loads(self.compose(args, timeout=15))

    def check_readiness(self):
        """Read current host, runtime and source-copy prerequisites without preparing an update."""
        require(os.name == 'posix' and os.geteuid() == 0, 'update_root_linux_required')
        check_docker(self.run)
        check_time_sync(self.run)
        config = json.loads(self.compose(['config', '--format', 'json']))
        validate_composed(config, self.plan.port, directory=self.root, project=self.project)
        current = self.containers()
        require(set(current) == {'db', 'backend'} and all(v['State']['Running'] for v in current.values()),
                'update_services_must_be_running')
        db_mounts = [v for v in current['db']['Mounts'] if v['Destination'] == '/var/lib/postgresql/data']
        require(len(db_mounts) == 1 and db_mounts[0]['Type'] == 'volume', 'update_database_mount_invalid')
        name = db_mounts[0]['Name']
        self.volume(name)
        users = self.docker(['ps', '-aq', '--no-trunc', '--filter', 'volume=' + name]).split()
        require(set(users) == {current['db']['Id']}, 'update_database_volume_shared')
        self.old_images = {service: item['Image'] for service, item in current.items()}
        require(all(IMAGE.fullmatch(v) for v in self.old_images.values()), 'update_image_identity_invalid')
        status = self.status()
        healthy_admission(status)
        require(status['hold']['state'] in {'absent', 'disabled'} and not status['setup_active']
                and status['release_version'] == self.plan.current_version
                and status['schema']['ready'] and status['schema']['version'] == self.plan.installed_schema.schema_version
                and status['schema']['baseline_id'] == self.plan.installed_schema.baseline_id,
                'update_runtime_not_ready')
        # The early source check refuses expired data before even preparing a candidate.
        if self.reader_source_root is None:
            window = json.loads(_window_read(lambda: self.compose(
                ['exec', '-T', 'backend', 'python', 'ops/update_copy_window.py'], timeout=WINDOW_READ_SECONDS + 30)))
        else:
            # This temporary reader runs only the bounded read-only source query.
            # It uses the old image's dependencies with captured signed source.
            window = self._source_window({'operation_id': secrets.token_hex(16)}, current,
                                         self.old_images['backend'])
        marker_from_window(window, created_at=int(self.clock()))
        return {'old_images': self.old_images, 'old_volume': name, 'runtime_epoch': status['runtime_epoch']}

    def preflight(self):
        observed = self.check_readiness()
        # This probe creates and exchanges disposable directories; --check must
        # not call it or imply that the filesystem has already been exercised.
        journal.preflight_exchange(self.layout)
        return observed

    def save(self, value):
        journal.save_journal(self.layout, value)

    def helper_name(self, value):
        return self.project + '-update-' + value['operation_id'] + '-helper'

    def helper(self, value, image, arguments, *, mounts=(), timeout=300, network='none', user='0:0', entrypoint='python', environment_file=None, capabilities=('DAC_OVERRIDE', 'CHOWN', 'FOWNER'), auto_remove=False):
        self.stop_helpers(value)
        name = self.helper_name(value)
        args = ['run', '--name', name, '--label', LABEL + '=' + value['operation_id'],
                '--label', 'org.leti-cook.project=' + self.project, '--network=' + network,
                '--user=' + user, '-e', 'LC_ALL=C', '--read-only', '--security-opt=no-new-privileges',
                '--cap-drop=ALL', *['--cap-add=' + capability for capability in capabilities],
                '--tmpfs=/tmp:rw,noexec,nosuid,size=16m,mode=1777', *HELPER_LIMITS]
        if auto_remove:
            args.append('--rm')
        for mount in mounts:
            args += ['--mount', mount]
        if environment_file is not None:
            require(environment_file == self.root / 'backend.env', 'update_helper_environment_invalid')
            args += ['--env-file', str(environment_file)]
        try:
            return self.docker(args + ['--entrypoint', entrypoint, image] + arguments, timeout=timeout)
        finally:
            self.stop_helpers(value)

    def stop_helpers(self, value):
        name = self.helper_name(value)
        matches = self.docker(['ps', '-aq', '--no-trunc', '--filter', 'name=^/' + name + '$']).split()
        for ident in matches:
            data = self.inspect('container', ident)
            labels = data.get('Config', {}).get('Labels', {})
            require(data['Name'] == '/' + name and labels.get(LABEL) == value['operation_id']
                    and labels.get('org.leti-cook.project') == self.project, 'update_helper_not_owned')
            self.docker(['rm', '-f', ident], timeout=30)

    def prepare(self, value):
        from ops.update_candidate import prepare_candidate
        self.check_space(value)
        prepared = prepare_candidate(self.plan, self.layout.root / 'candidate')
        identities = journal.tree_identity(prepared.path)
        images = {}
        arch = json.loads(self.docker(['info', '--format', '{{json .Architecture}}']))
        for service in ('db', 'backend'):
            if self.build_local or arch not in {'amd64', 'x86_64'}:
                tag = self.project + '-update-' + value['operation_id'] + '-' + service
                context = prepared.path / ('database' if service == 'db' else 'application')
                self.docker(['build', '--label', 'org.leti-cook.project=' + self.project,
                             '--label', LABEL + '=' + value['operation_id'], '--tag', tag, str(context)], timeout=1800)
                reference = tag
            else:
                image = self.release.manifest['images'][service]
                reference = image['repository'] + '@' + image['digest']
                try:
                    images[service] = ensure_release_image(reference, self.run)
                except InstallError as error:
                    if str(error) in {'release_image_identity_invalid', 'release_image_cache_unavailable'}:
                        raise UpdateError(str(error).replace('release_', 'update_', 1)) from None
                    raise
                continue
            images[service] = self.inspect('image', reference)['Id']
            require(IMAGE.fullmatch(images[service]), 'update_image_identity_invalid')
        self.check_space(value)
        from ops.update_candidate import preflight_candidate
        repeated = preflight_candidate(self.root, self.release,
                                       allow_prerelease=bool(parse_version(self.plan.target_version).prerelease))
        require(repeated.installed_fingerprint == self.plan.installed_fingerprint,
                'update_installation_changed_during_preparation')
        write_override(prepared.path, images, value['old_volume'])
        return {'new_tree': identities, 'new_images': images}

    def check_space(self, value):
        # Fixed trusted script counts regular files only. The authoritative archive
        # enforces these ceilings again after shutdown; growth fails before swap.
        script = (
            'import os,json,stat\nsize=0;count=0\n'
            'for root,dirs,files in os.walk("/source",followlinks=False):\n'
            ' for name in dirs+files:\n'
            '  s=os.lstat(os.path.join(root,name))\n'
            '  if not(stat.S_ISDIR(s.st_mode) or stat.S_ISREG(s.st_mode)): raise ValueError("special")\n'
            '  count+=1;size+=s.st_size if stat.S_ISREG(s.st_mode) else 0\n'
            'print(json.dumps({"bytes":size,"entries":count}))\n')
        sizes = json.loads(self.helper(value, value['old_images']['backend'], ['-c', script],
                                      mounts=['type=volume,source=' + value['old_volume'] + ',target=/source,readonly']))
        maximum = max(64 * 1024 ** 2, sizes['bytes'] * 2 + (sizes['entries'] + 1000) * 1024)
        entries = max(10000, sizes['entries'] * 2 + 1000)
        require(maximum <= MAX_COPY and entries <= MAX_ENTRIES, 'update_copy_limit_exceeded')
        self.sizes = maximum, entries
        docker_root = json.loads(self.docker(['info', '--format', '{{json .DockerRootDir}}']))
        require(Path(docker_root).is_absolute(), 'update_docker_storage_invalid')
        # Reserve an extra database volume for rollback as well as ciphertext.
        require(shutil.disk_usage(self.layout.root).free >= maximum + SPACE_MARGIN
                and shutil.disk_usage(docker_root).free >= maximum * 2 + SPACE_MARGIN,
                'update_insufficient_disk_space')

    def hold(self, value):
        journal.write_hold(self.layout, value['operation_id'])

    def drain(self, value):
        deadline = time.monotonic() + 660
        while time.monotonic() < deadline:
            status = self.status(value['operation_id'])
            healthy_admission(status, held=True)
            require(status['runtime_epoch'] == value['runtime_epoch'], 'update_runtime_restarted_during_drain')
            require(not status['setup_active'], 'update_setup_in_progress')
            collector = status['collector']
            require(collector['outcome'] not in {'forced', 'unconfirmed'}, 'update_collection_stop_unconfirmed')
            if (collector['observed_operation_id'] == value['operation_id'] and not collector['active']
                    and collector['outcome'] in {'normal', 'idle'}):
                return status['runtime_epoch']
            self.pause(1)
        raise UpdateError('update_collection_stop_timeout')

    def stop_service(self, service, *, clean=True):
        current = self.containers()
        if service not in current:
            return
        ident = current[service]['Id']
        # Survives a host reboot while the command is interrupted.
        self.docker(['update', '--restart=no', ident])
        self.docker(['stop', '--time', '60', ident], timeout=75)
        stopped = self.inspect('container', ident)
        require(not stopped['State']['Running'] and (not clean or stopped['State']['ExitCode'] == 0),
                'update_' + service + '_stop_not_clean')

    def stop_backend(self, value):
        self.stop_service('backend')

    def stop_database(self, value):
        self.stop_service('db')
        self.volume_idle(value['old_volume'])

    def stop_all(self, value):
        self.stop_service('backend', clean=False)
        self.stop_service('db', clean=False)

    def window(self, value):
        current = self.containers()
        require('backend' not in current or not current['backend']['State']['Running'], 'update_writer_still_running')
        if self.reader_source_root is not None:
            return self._source_window(value, current, value['old_images']['backend'])
        network = self._window_network(current)
        raw = _window_read(lambda: self.helper(
            value, value['old_images']['backend'], ['-B', 'ops/update_copy_window.py'],
            network=network, user='10001:10001', timeout=WINDOW_READ_SECONDS + 30,
            environment_file=self.root / 'backend.env',
            mounts=['type=bind,source=' + str(self.root / 'config') + ',target=/etc/leti-cook,readonly']))
        return json.loads(raw)

    def _window_network(self, current):
        networks = current['db']['NetworkSettings']['Networks']
        require(len(networks) == 1, 'update_database_network_invalid')
        network = next(iter(networks))
        details = self.inspect('network', network)
        require(details['Internal'] and details.get('Labels', {}).get(PROJECT_LABEL) == self.project,
                'update_database_network_not_owned')
        require(set(details.get('Containers', {})) <= {item['Id'] for item in current.values()},
                'update_database_network_shared')
        return network

    def _source_window(self, value, current, image):
        from ops.update_reader import validate_reader_source
        source = validate_reader_source(self.reader_source_root, self.release, self.plan)
        require('db' in current and current['db']['State']['Running'], 'update_database_not_running')
        network = self._window_network(current)
        raw = _window_read(lambda: self.helper(
            value, image, ['-B', '/verified-reader/ops/update_copy_window.py'],
            network=network, user='10001:10001', timeout=WINDOW_READ_SECONDS + 30,
            environment_file=self.root / 'backend.env', capabilities=(), auto_remove=True,
            mounts=['type=bind,source=' + str(self.root / 'config') + ',target=/etc/leti-cook,readonly',
                    'type=bind,source=' + str(source) + ',target=/verified-reader,readonly']))
        return json.loads(raw)

    def copy_parameters(self, value, window):
        check_time_sync(self.run)
        marker = marker_from_window(window, created_at=int(self.clock()))
        require(self.sizes is not None, 'update_copy_limits_missing')
        return {'schema_version': self.plan.installed_schema.schema_version,
                'created_at': marker.created_at, 'expires_at': marker.expires_at,
                'max_bytes': self.sizes[0], 'max_files': self.sizes[1]}

    def crypto(self, value, action, *, target=None):
        data = value['copy']
        args = ['ops/update_copy_crypto.py', action, '--ciphertext',
                '/copy/copy.partial' if action in {'create', 'verify'} else '/copy/copy.enc',
                '--key-file', '/key/copy.key', '--marker-file', '/marker/marker.json',
                '--copy-id', value['operation_id'], '--database-id', value['database_id'],
                '--schema-version', str(data['schema_version']), '--max-bytes', str(data['max_bytes']),
                '--max-files', str(data['max_files'])]
        mounts = ['type=bind,source=' + str(self.layout.keys) + ',target=/key,readonly',
                  'type=bind,source=' + str(self.layout.marker_directory) + ',target=/marker,readonly',
                  'type=bind,source=' + str(self.layout.cipher_directory) + ',target=/copy' + ('' if action == 'create' else ',readonly')]
        if action == 'create':
            args += ['--source', '/source']
            mounts += ['type=volume,source=' + value['old_volume'] + ',target=/source,readonly']
        elif action == 'restore':
            args += ['--target', '/target']
            mounts += ['type=volume,source=' + target + ',target=/target']
        return json.loads(self.helper(value, value['old_images']['backend'], args, mounts=mounts, timeout=3600))

    def copy(self, value):
        data = value['copy']
        self.volume_idle(value['old_volume'])
        control = self.helper(value, value['old_images']['db'], ['/source'],
                              mounts=['type=volume,source=' + value['old_volume'] + ',target=/source,readonly'],
                              entrypoint='pg_controldata')
        require(re.search(r'^Database cluster state:\s+shut down\s*$', control, re.MULTILINE),
                'update_postgres_not_cleanly_stopped')
        reservation = arm_copy(self.layout, copy_id=value['operation_id'], schema_version=data['schema_version'],
                               marker=Marker(data['created_at'], data['expires_at']))
        created = self.crypto(value, 'create')
        verified = self.crypto(value, 'verify')
        require(created == verified, 'update_copy_verification_mismatch')
        publish_copy(reservation)

    def exchange(self, value, *, desired):
        journal.exchange_trees(self.layout, value['old_tree'], value['new_tree'], desired=desired)

    def select_runtime(self, value, *, new):
        images = value['new_images'] if new else value['old_images']
        volume = value['old_volume'] if new else value['restore_volume'] or value['old_volume']
        self.volume(volume)
        write_override(self.root, images, volume)

    def start_database(self, value, *, new):
        self.compose(['up', '-d', '--no-build', '--force-recreate', '--wait', '--wait-timeout', '180', 'db'], timeout=240)

    def prepare_channel_add_state(self, value, *, new):
        from ops.update_candidate import channel_add_owner_present
        expected_tree = value['new_tree'] if new else value['old_tree']
        require(journal.tree_identity(self.root) == expected_tree, 'update_tree_identity_mismatch')
        if not channel_add_owner_present(self.root):
            return  # A previous legacy binary has no persistent admission owner.
        transition = False
        if new:
            require(value['phase'] == 'new_start_intent'
                    and journal.tree_position(self.layout, value['old_tree'], value['new_tree']) == 'new',
                    'update_admission_transition_intent_required')
            transition = not channel_add_owner_present(self.layout.root / 'candidate')
        self.stop_helpers(value)
        name = self.project + '_application-state'
        volume = self.inspect('volume', name)
        labels = volume.get('Labels') or {}
        require(volume.get('Name') == name and labels.get(PROJECT_LABEL) == self.project
                and labels.get('com.docker.compose.volume') == 'application-state'
                and volume.get('Driver') == 'local' and not volume.get('Options'),
                'update_application_state_not_owned')
        current = self.containers()
        backend = current.get('backend')
        expected_users = set()
        if backend is not None:
            require(not backend['State']['Running'], 'update_application_state_writer_active')
            mounts = [v for v in backend['Mounts'] if v['Destination'] == '/var/lib/leti-cook']
            require(len(mounts) == 1 and mounts[0]['Type'] == 'volume' and mounts[0]['Name'] == name,
                    'update_application_state_mount_invalid')
            expected_users.add(backend['Id'])
        users = self.docker(['ps', '-aq', '--no-trunc', '--filter', 'volume=' + name]).split()
        require(set(users) == expected_users, 'update_application_state_shared')
        require(not self.docker(['ps', '-q', '--no-trunc', '--filter', 'volume=' + name]).split(),
                'update_application_state_writer_active')
        image = value['new_images' if new else 'old_images']['backend']
        require(IMAGE.fullmatch(image), 'update_image_identity_invalid')
        arguments = ['-B', 'ops/self_hosted_owner.py', 'prepare-channel-add-state']
        if transition:
            arguments.append('--legacy-transition')
        result = json.loads(self.helper(value, image, arguments,
            mounts=['type=volume,source=' + name + ',target=/var/lib/leti-cook,volume-nocopy'],
            timeout=15, user='10001:10001', capabilities=(), environment_file=self.root / 'backend.env'))
        require(result == {'status':'channel_add_state_ready'}, 'update_channel_add_state_unconfirmed')

    def start_backend(self, value, *, new):
        self.prepare_channel_add_state(value, new=new)
        self.compose(['up', '-d', '--no-deps', '--no-build', '--force-recreate', '--wait', '--wait-timeout', '180', 'backend'], timeout=240)

    def schema(self, value):
        plan = self.plan.schema
        def run(sql, *, timeout):
            return self.compose(['exec', '-T', 'db', *PSQL_ARGUMENTS], timeout=timeout, input_data=sql)
        try:
            run_schema_upgrade(plan, expected_current_version=value['copy']['schema_version'], command=run)
        except ValueError:
            state = reconcile_schema_upgrade(plan, command=run)
            require(state.current_version == plan.schema_version, 'update_schema_not_completed')

    def health(self, value, *, new):
        deadline = time.monotonic() + 180
        while True:
            try:
                self.health_once(value, new=new)
                return
            except (ValueError, KeyError, OSError, RuntimeError):
                if time.monotonic() >= deadline:
                    raise UpdateError('update_runtime_health_failed') from None
                self.pause(1)

    def health_once(self, value, *, new):
        expected = value['new_version'] if new else value['old_version']
        status = self.status(value['operation_id'])
        healthy_admission(status, held=True, startup=True)
        require(status['release_version'] == expected and status['schema']['ready']
                and status['hold']['state'] == 'held' and not status['collector']['active']
                and not status['setup_active'], 'update_runtime_health_failed')
        # Strict full schema identity remains owned by the actual runtime guard.
        metadata = json.loads((self.root / 'application/schema-release.json').read_bytes())
        require(status['schema']['version'] == metadata['schema_version']
                and status['schema']['baseline_id'] == metadata['baseline_id'], 'update_schema_health_failed')
        containers = self.containers()
        require(set(containers) == {'db', 'backend'}, 'update_service_set_changed')
        expected_images = value['new_images'] if new else value['old_images']
        expected_volume = value['old_volume'] if new else value['restore_volume'] or value['old_volume']
        for service, item in containers.items():
            require(item['Image'] == expected_images[service], 'update_running_image_mismatch')
            require(item['State']['Running'] and item['State'].get('Health', {}).get('Status') == 'healthy',
                    'update_container_unhealthy')
        mounts = [item for item in containers['db']['Mounts'] if item['Destination'] == '/var/lib/postgresql/data']
        require(len(mounts) == 1 and mounts[0]['Type'] == 'volume' and mounts[0]['Name'] == expected_volume,
                'update_running_volume_mismatch')

    def remove_volume(self, name):
        self.volume_idle(name)
        require(not self.docker(['ps', '-aq', '--filter', 'volume=' + name]).split(), 'update_volume_still_attached')
        self.docker(['volume', 'rm', name])

    def check_restore(self, value):
        check_time_sync(self.run)
        data = value['copy']
        return validate_restore(self.layout, expected_copy_id=value['operation_id'],
                         expected_schema_version=data['schema_version'],
                         expected_marker=Marker(data['created_at'], data['expires_at']), time_trusted=True)

    def restore(self, value):
        self.check_restore(value)
        name = self.project + '_restore_' + value['operation_id']
        self.stop_helpers(value)
        # Remove stopped service containers so an interrupted partial restore can
        # be discarded. The original volume and application state remain intact.
        for item in self.containers().values():
            require(not item['State']['Running'], 'update_restore_writer_active')
            self.docker(['rm', item['Id']])
        existing = self.docker(['volume', 'ls', '-q', '--filter', 'name=^' + name + '$']).split()
        if existing:
            require(existing == [name], 'update_restore_volume_ambiguous')
            require(self.volume(name)['Labels'].get(LABEL) == value['operation_id'], 'update_restore_volume_not_owned')
            self.remove_volume(name)
        self.docker(['volume', 'create', '--label', PROJECT_LABEL + '=' + self.project,
                     '--label', 'com.docker.compose.volume=database-data', '--label', LABEL + '=' + value['operation_id'], name])
        self.volume_idle(name)
        self.crypto(value, 'restore', target=name)
        return name

    def recover_original(self, value):
        if value['phase'] not in EARLY:
            self.stop_all(value)
            self.select_runtime(value, new=False)
            self.start_database(value, new=False)
            self.start_backend(value, new=False)
            self.health(value, new=False)

    def cleanup_preparation(self, value):
        if value['phase'] == 'preparing' and value['new_tree'] is None:
            journal.cleanup_preparing_candidate(self.layout)

    def cleanup(self, value, *, new):
        self.stop_helpers(value)
        purge_copy(self.layout, expected_copy_id=value['operation_id'], force=True)
        if not new and value['restore_volume']:
            existing = self.docker(['volume', 'ls', '-q', '--filter', 'name=^' + value['old_volume'] + '$']).split()
            if existing:
                self.remove_volume(value['old_volume'])
        if value['new_tree'] is not None:
            expected = value['old_tree'] if new else value['new_tree']
            candidate = self.layout.root / 'candidate'
            if candidate.exists():
                journal.remove_tree(self.layout, expected)
        journal.remove_hold(self.layout, value['operation_id'])
