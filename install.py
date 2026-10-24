"""Install Leti Cook into a new folder on this computer.

Only a fresh installation is supported: an existing installation folder is never
changed, upgraded or reused.
"""
from __future__ import annotations
import argparse
import getpass
import warnings
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import socket
import subprocess
import sys
import urllib.request

# Standalone release entry: do not add bytecode files to a checksummed bundle.
sys.dont_write_bytecode = True
if not __package__:
    package_root = Path(__file__).resolve().parent
    if not (package_root / 'ops/self_hosted_credentials.py').is_file():
        package_root = package_root.parent
    sys.path.insert(0, str(package_root))
from ops.self_hosted_credentials import runtime_settings
from ops.bundle_contract import FORMAT, MAX_FILE, MAX_TOTAL, MAX_FILES, REQUIRED_BUNDLE_FILES
from application.release_version import parse_version_file
from ops.schema_manifest import validate_schema_bundle
DEFAULT_DIRECTORY = "/opt/leti-cook"
DEFAULT_PORT = 8002


class InstallError(RuntimeError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 1024):
            raise InstallError('linked_installation_path_refused')
    return path


def verify_bundle(bundle):
    bundle = safe_path(bundle)
    raw = (bundle / 'bundle.json').read_bytes()
    if len(raw) > 256 * 1024:
        raise InstallError('bundle_manifest_too_large')
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or manifest.get('format') != FORMAT or manifest.get('version') != 1
            or manifest.get('fresh_install_only') is not True):
        raise InstallError('invalid_bundle_manifest')
    hashes = manifest.get('files')
    if not isinstance(hashes, dict) or not 10 <= len(hashes) <= MAX_FILES:
        raise InstallError('invalid_bundle_files')
    files = {}
    total = 0
    for name, expected in hashes.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            raise InstallError('invalid_bundle_path')
        path = PurePosixPath(name)
        if (path.is_absolute() or '..' in path.parts or '\\' in name
                or any(part.startswith('.') for part in path.parts) or ':' in name
                or not re.fullmatch(r'[0-9a-f]{64}', expected or '')):
            raise InstallError('invalid_bundle_path')
        source = safe_path(bundle / name)
        if not source.is_file() or source.stat().st_nlink != 1 or source.stat().st_size > MAX_FILE:
            raise InstallError('invalid_bundle_file')
        data = source.read_bytes()
        total += len(data)
        if total > MAX_TOTAL or sha(data) != expected:
            raise InstallError('bundle_digest_mismatch')
        files[name] = data
    # Exact file set: reject accidentally packaged keys, dumps and extra files.
    actual = {p.relative_to(bundle).as_posix() for p in bundle.rglob('*') if p.is_file()}
    if actual != set(files) | {'bundle.json'}:
        raise InstallError('unlisted_bundle_files')
    for name in files:
        if name.endswith(('.env', '.pem', '.key', '.sqlite3')) or 'node_modules' in name or '/owner_studio/' in name:
            raise InstallError('private_bundle_file')
    if not REQUIRED_BUNDLE_FILES <= set(files):
        raise InstallError('incomplete_bundle')
    parse_version_file(files['application/VERSION'])
    try:
        validate_schema_bundle(files)
    except ValueError:
        raise InstallError('invalid_fresh_schema') from None
    return manifest, files


def installation_settings(directory, port, schema_hash, project):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise InstallError('port_out_of_range')
    if not re.fullmatch(r'leti-cook-[0-9a-f]{12}', project):
        raise InstallError('isolated_project_required')
    directory = safe_path(directory)
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', directory.as_posix()):
        raise InstallError('simple_absolute_linux_directory_required')
    authenticator_password = secrets.token_hex(32)
    values = {'SELF_HOSTED_DB_PROJECT': project, 'LIC_HTTP_PORT': str(port),
              'LIC_UPDATE_MARKER_HOST_DIRECTORY': str(directory) + '.update/marker',
              'LIC_UPDATE_CIPHER_HOST_DIRECTORY': str(directory) + '.update/cipher',
              'SELF_HOSTED_SCHEMA_PATH': str(directory / 'schema/schema.sql'),
              'SELF_HOSTED_SCHEMA_SHA256': schema_hash,
              'POSTGRES_PASSWORD': secrets.token_hex(32), 'AUTHENTICATOR_PASSWORD': authenticator_password}
    backend = {'LIC_DEPLOYMENT_PROFILE': 'self-hosted', 'PYTHON_DOTENV_DISABLED': '1',
               **runtime_settings(authenticator_password),
               'LIC_HOT_RUNTIME_URL': 'http://127.0.0.1:8002/internal/hot-runtime',
               'LIC_PUBLIC_ORIGIN': f'http://127.0.0.1:{port}',
               'LIC_AUTH_DATABASE': '/var/lib/leti-cook/access/owner.sqlite3',
               'LIC_SETUP_DIRECTORY': '/var/lib/leti-cook/configuration',
               'LIC_SETTINGS_FILE': '/etc/leti-cook/config.ini',
               'LIC_UPDATE_MARKER_DIRECTORY': '/run/leti-cook-update/marker',
               'LIC_UPDATE_CIPHER_DIRECTORY': '/run/leti-cook-update/cipher',
               'OPERATOR_JOB_WAKE_PATH': '/var/lib/leti-cook/operator-jobs.wake',
               'DATABASE_VIDEO_COLLECTION_ENABLED': 'true',
               'DATABASE_CHANNEL_DISCOVERY_ENABLED': 'true',
               'TYPED_PLAYLIST_INVENTORY_ENABLED': 'true',
               'SHORT_VIDEO_COLLECTION_ENABLED': 'true',
               'COMPLETED_STREAM_COLLECTION_ENABLED': 'true',
               'OPERATOR_JOBS_MODE': 'durable',
               'YOUTUBE_QUOTA_PROJECT_ID': 'self-hosted',
               'YOUTUBE_BATCH_STATS_ENABLED': '1',
               'CHANNEL_BASELINE_ANALYTICS_ENABLED': '1', 'CHANNEL_DERIVED_INSIGHTS_ENABLED': '1'}
    return values, backend


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def write_release_file(directory, name, data):
    """Public build/bind input stays readable; the installation root stays 0700."""
    target = directory / name
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    for parent in target.parents:
        if parent == directory:
            break
        parent.relative_to(directory)
        parent.chmod(0o755)
    write_private(target, data)
    target.chmod(0o644)


def write_env(path, values):
    write_private(path, ''.join(k + '=' + v + '\n' for k, v in values.items()).encode())


def command(args, *, cwd=None, interactive=False, timeout=300, input_data=None):
    if interactive:
        # Docker forwards its container TTY through the client stdout. Bind the
        # client to the host terminal too, so `install.sh | tee ...` cannot log
        # a setup code even though it was written to /dev/tty inside Docker.
        # A terminal is not seekable; buffered r+ raises UnsupportedOperation.
        with open('/dev/tty', 'r+b', buffering=0) as terminal:
            if not terminal.isatty():
                raise InstallError('interactive_terminal_required')
            result = subprocess.run(args, cwd=cwd, stdin=terminal, stdout=terminal,
                                    stderr=terminal, text=True, timeout=timeout)
    else:
        result = subprocess.run(args, cwd=cwd,
                                **({'input': input_data} if input_data is not None else {'stdin': subprocess.DEVNULL}),
                                capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Classify only the known Docker diagnostic; captured output can contain
        # credentials and must never become a message or exception payload.
        if (not interactive and args and args[0] == 'docker'
                and isinstance(result.stderr, str)
                and 'all predefined address pools have been fully subnetted' in result.stderr.lower()):
            raise InstallError('docker_network_address_pool_exhausted')
        raise InstallError('command_failed_' + str(result.returncode))
    return result.stdout if not interactive else ''


def compose_prefix(directory, project):
    prefix = ['docker', 'compose', '--project-name', project, '--env-file', str(directory / 'installation.env'),
              '-f', str(directory / 'database/compose.yaml'), '-f', str(directory / 'application.compose.yaml')]
    override = directory / 'runtime.compose.json'
    if override.exists():
        safe_path(override)
        prefix += ['-f', str(override)]
    return prefix


def validate_composed(config, port, *, directory, project):
    services = config.get('services', {})
    if set(services) != {'db', 'backend'}:
        raise InstallError('unexpected_service_set')
    if services['db'].get('ports'):
        raise InstallError('database_port_exposed')
    networks = config.get('networks', {})
    if (networks.get('database', {}).get('internal') is not True
            or set(services['db'].get('networks', {})) != {'database'}
            or set(services['backend'].get('networks', {})) != {'database', 'application-access'}
            or networks.get('application-access', {}).get('internal', False)):
        raise InstallError('private_database_network_required')
    if services['backend'].get('depends_on', {}).get('db', {}).get('condition') != 'service_healthy':
        raise InstallError('healthy_database_dependency_required')
    ports = services['backend'].get('ports', [])
    if (len(ports) != 1 or ports[0].get('host_ip') != '127.0.0.1' or str(ports[0].get('published')) != str(port)
            or ports[0].get('target') != 8002):
        raise InstallError('only_loopback_frontend_may_be_published')
    for service in services.values():
        if service.get('logging', {}).get('driver') != 'none':
            raise InstallError('persistent_docker_logs_refused')
        if service.get('network_mode') or service.get('pid') == 'host':
            raise InstallError('host_namespace_refused')
    if services['backend'].get('privileged') or services['backend'].get('user') != '10001:10001':
        raise InstallError('unprivileged_backend_required')
    for volume in config.get('volumes', {}).values():
        if volume.get('external'):
            raise InstallError('external_volume_refused')
    # The runtime sees only its usual state/config and the two narrow cleanup
    # mounts. It must never receive a key, the owner journal or the host root.
    expected_mounts = {
        '/var/lib/leti-cook': ('volume', 'application-state', False),
        '/etc/leti-cook': ('bind', str(directory / 'config'), True),
        '/run/leti-cook-update/marker': ('bind', str(directory) + '.update/marker', True),
        '/run/leti-cook-update/cipher': ('bind', str(directory) + '.update/cipher', False),
    }
    mounts = services['backend'].get('volumes', [])
    if len(mounts) != len(expected_mounts):
        raise InstallError('unexpected_backend_mounts')
    seen = set()
    for mount in mounts:
        target = mount.get('target')
        if target in seen or target not in expected_mounts:
            raise InstallError('unexpected_backend_mounts')
        seen.add(target)
        kind, source, readonly = expected_mounts[target]
        if (mount.get('type'), mount.get('source'), mount.get('read_only', False)) != (kind, source, readonly):
            raise InstallError('unexpected_backend_mounts')
        if target.startswith('/run/leti-cook-update/') and mount.get('bind', {}).get('create_host_path') is not False:
            raise InstallError('update_copy_mount_must_exist')
    if config.get('volumes', {}).get('application-state', {}).get('name') != project + '_application-state':
        raise InstallError('unexpected_application_state_volume')


def check_update_copy_storage(directory):
    from ops.update_copy_store import validate_new_layout
    try:
        validate_new_layout(directory)
    except (OSError, ValueError):
        raise InstallError('update_copy_storage_unavailable') from None


def initialize_update_copy_storage(directory, project, database_id):
    from ops.update_copy_store import prepare_layout
    try:
        prepare_layout(directory, project=project, database_id=database_id)
    except (OSError, ValueError):
        raise InstallError('update_copy_storage_unavailable') from None


def check_docker(run=command):
    if sys.platform != 'linux':
        raise InstallError('linux_installer_required')
    version = run(['docker', 'compose', 'version', '--short']).strip().lstrip('v')
    match = re.match(r'(\d+)\.(\d+)\.(\d+)', version)
    if not match or tuple(map(int, match.groups())) < (2, 24, 4):
        raise InstallError('compose_2_24_4_required')
    # Bind paths and resource checks describe THIS machine, not a remote daemon.
    endpoint = os.environ.get('DOCKER_HOST', '')
    if not endpoint or os.environ.get('DOCKER_CONTEXT'):
        endpoint = run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}']).strip()
    if not endpoint.startswith('unix://'):
        raise InstallError('local_docker_socket_required')
    info = json.loads(run(['docker', 'info', '--format', '{{json .}}']))
    if not isinstance(info, dict) or info.get('OSType') != 'linux':
        raise InstallError('linux_docker_required')
    return info


# Shown after reason=<code>; the code alone says what failed, this says what to do.
REASON_HINTS = {
    'docker_network_address_pool_exhausted': (
        'Docker has no address pool available for a new network. Review unused Docker networks '
        'or ask the server administrator to expand Docker default address pools, then retry. '
        'The installer does not remove networks automatically.'),
    'update_copy_storage_unavailable': (
        'Choose a new installation folder under a root-owned directory such as /opt. '
        'Its .update and .update-keys sibling folders must not already exist. '
        'Keep existing folders until their installation and recovery status are checked.'),
    'clock_not_synchronized': (
        'The system clock is not synchronised. Leti Cook measures the age of YouTube data with it. '
        'Turn on time synchronisation with: sudo timedatectl set-ntp true. If it answers that NTP is not '
        'supported, install a time service first (Ubuntu or Debian: sudo apt-get install systemd-timesyncd). '
        'Wait until timedatectl shows "System clock synchronized: yes", then run the installer again.'),
    'clock_sync_unverifiable': (
        'timedatectl could not say whether the system clock is synchronised. Leti Cook needs a Linux '
        'host with systemd that keeps its clock synchronised, for example with systemd-timesyncd or chrony. '
        'Check that "timedatectl show -p NTPSynchronized --value" prints yes, then run the installer again.'),
}


def check_time_sync(run=command):
    """Refuse a host whose clock is not kept synchronised (NTP).

    The application measures the age of YouTube data with this clock. Without
    systemd's answer the result is unknown, and unknown is refused as well.
    """
    try:
        value = run(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value'], timeout=15)
    except (InstallError, OSError, subprocess.SubprocessError):
        raise InstallError('clock_sync_unverifiable') from None
    value = value.strip() if isinstance(value, str) else ''
    if value == 'no':
        raise InstallError('clock_not_synchronized')
    if value != 'yes':
        raise InstallError('clock_sync_unverifiable')


def validate_port(port):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise InstallError('port_out_of_range')
    return port


def port_available(port):
    validate_port(port)
    try:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
        return True
    except OSError:
        return False


def available_port(start=DEFAULT_PORT, *, available=port_available):
    # Bounded selection only for a NEW installation; never renumber an existing one.
    for port in range(validate_port(start), min(start + 100, 65536)):
        if available(port):
            return port
    raise InstallError('no_free_port_in_suggestion_range_choose_port')


def new_directory(value):
    # Avoid environment expansion/Compose interpolation and keep paths portable.
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(value)) or '..' in PurePosixPath(value).parts:
        raise InstallError('simple_absolute_linux_directory_required')
    directory = safe_path(value)
    if directory.exists():
        raise InstallError('new_directory_required_no_automatic_overwrite')
    if not directory.parent.is_dir():
        raise InstallError('installation_parent_must_exist')
    if not os.access(directory.parent, os.W_OK | os.X_OK):
        raise InstallError('installation_parent_not_writable')
    return directory


def ask_value(label, default, validate, *, ask=input, emit=print):
    while True:
        suggestion = f' [{default}] (Enter to use this, or type another)' if str(default) else ''
        value = ask(f'{label}{suggestion}: ').strip() or str(default)
        try:
            return validate(value)
        except (InstallError, ValueError) as error:
            reason = str(error) if isinstance(error, InstallError) else 'enter_a_number'
            emit('Invalid choice: ' + reason)
            if reason in REASON_HINTS:
                emit(REASON_HINTS[reason])


def choose_plan(args, *, ask=input, emit=print, available=port_available):
    def folder(value):
        directory = new_directory(value)
        check_update_copy_storage(directory)
        return directory

    directory = (folder(args.directory) if args.directory is not None else
                 ask_value('Installation folder', DEFAULT_DIRECTORY, folder, ask=ask, emit=emit))

    def free_port(value):
        port = validate_port(int(value))
        if not available(port):
            raise InstallError('port_in_use_choose_another')
        return port
    if args.port is not None:
        port = free_port(args.port)  # An explicit request is never silently replaced.
    else:
        try:
            suggested = available_port(available=available)
        except InstallError:
            suggested = ''
        port = ask_value('Application port', suggested, free_port, ask=ask, emit=emit)
    return {'directory': directory, 'port': port}


def report_resources(directory, info, release_bytes, *, emit=print):
    gib = 1024 ** 3
    free = shutil.disk_usage(directory.parent).free
    emit(f'Free disk at installation folder: {free / gib:.1f} GiB')
    if free < release_bytes:
        raise InstallError('insufficient_disk_for_release')
    # Report real resources, not a fabricated guaranteed capacity or runtime cap.
    docker_root = info.get('DockerRootDir', '')
    try:
        docker_free = shutil.disk_usage(docker_root).free if docker_root else None
    except OSError:
        docker_free = None
    emit('Free disk for Docker: '
         + (f'{docker_free / gib:.1f} GiB' if docker_free is not None else 'not readable; check Docker storage'))
    memory = {}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            fields = line.split()
            if fields[0] in ('MemTotal:', 'MemAvailable:'):
                memory[fields[0]] = int(fields[1]) * 1024
    except (OSError, ValueError):
        pass
    emit('RAM available: '
         + (f"{memory['MemAvailable:'] / gib:.1f} GiB" if 'MemAvailable:' in memory else 'unknown'))
    emit('Images, database growth and other running services need additional disk and RAM.')


def print_address(port, *, emit=print):
    emit(f'Local browser address: http://127.0.0.1:{port}/auth')
    emit('This address works on the computer where the application is installed.')


def print_plan(plan, info, release_bytes, *, emit=print):
    report_resources(plan['directory'], info, release_bytes, emit=emit)
    emit('\nInstallation summary')
    emit('Folder: ' + str(plan['directory']))
    print_address(plan['port'], emit=emit)
    emit('Enter your YouTube API key to start installation. Ctrl+C cancels.')


def hidden_api_key():
    if not sys.stdin.isatty():
        raise InstallError('interactive_terminal_required_for_key')
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            value = getpass.getpass('YouTube API key (hidden): ')
        except getpass.GetPassWarning:
            raise InstallError('hidden_key_input_unavailable') from None
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,200}', value):
        raise InstallError('invalid_youtube_key_format')
    return value


def install(bundle, directory, port, *, run=command, api_key=None):
    if sys.platform != 'linux':
        raise InstallError('linux_installer_required')
    manifest, files = verify_bundle(bundle)
    return install_files(manifest, files, directory, port, run=run, api_key=api_key)


def install_files(manifest, files, directory, port, *, run=command, api_key=None,
                  source='source', prepare_images=None, bundle_bytes=None):
    """Install captured, previously verified bytes; never reopen a package.

    This is an internal host API. Both public entrypoints verify their input
    first; an image preparer is supplied only by the trusted release installer.
    """
    if sys.platform != 'linux':
        raise InstallError('linux_installer_required')
    if source not in {'source', 'release'} or (source == 'release') != (prepare_images is not None):
        raise InstallError('installation_source_invalid')
    directory = safe_path(directory)
    if directory.exists():
        raise InstallError('new_directory_required_no_automatic_overwrite')
    # Refuse before creating files/containers if Docker/Compose/clock/port is unavailable.
    docker_info = check_docker(run)
    check_time_sync(run)
    directory = new_directory(directory)
    if not port_available(port):
        raise InstallError('port_in_use_choose_another')
    if api_key is not None and not re.fullmatch(r'[A-Za-z0-9_-]{20,200}', api_key):
        raise InstallError('invalid_youtube_key_format')
    check_update_copy_storage(directory)
    project = 'leti-cook-' + secrets.token_hex(6)
    # Random names are still checked; never adopt any existing installation.
    for resource in ('container', 'volume', 'network'):
        if run(['docker', resource, 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + project]).strip():
            raise InstallError('project_collision')
    schema = json.loads(files['schema/manifest.json'])
    version = parse_version_file(files['application/VERSION'])
    settings, backend = installation_settings(directory, port, schema['files']['schema.sql'], project)
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, data in files.items():
        # No credentials in the verified release tree; create them below.
        write_release_file(directory, name, data)
    write_private(directory / 'bundle.json', bundle_bytes if bundle_bytes is not None
                  else json.dumps(manifest, sort_keys=True).encode())
    write_env(directory / 'installation.env', settings)
    write_env(directory / 'backend.env', backend)
    write_private(directory / 'installation.json', json.dumps({
        'format': FORMAT, 'project': project, 'port': port, 'status': 'preparing',
        'database_id': backend['LIC_DATABASE_ID'],
        'version': version, 'source': source,
        'bundle_sha256': sha((directory / 'bundle.json').read_bytes())}).encode())
    initialize_update_copy_storage(directory, project, backend['LIC_DATABASE_ID'])
    prefix = compose_prefix(directory, project)
    # Never print config: its resolved environment includes credentials.
    validate_composed(json.loads(run(prefix + ['config', '--format', 'json'], cwd=directory)), port,
                      directory=directory, project=project)

    def stage(name, args, timeout=300):
        print('stage=' + name, flush=True)
        run(prefix + args, cwd=directory, timeout=timeout)
    if prepare_images is None:
        stage('build_images', ['build', 'db', 'backend'], timeout=900)
    else:
        images = prepare_images(directory, project, docker_info, run)
        prefix = compose_prefix(directory, project)
        resolved = json.loads(run(prefix + ['config', '--format', 'json'], cwd=directory))
        validate_composed(resolved, port, directory=directory, project=project)
        if (set(images) != {'db', 'backend'}
                or any(resolved['services'][name].get('image') != image for name, image in images.items())
                or resolved.get('volumes', {}).get('database-data', {}).get('name') != project + '_database-data'):
            raise InstallError('release_image_override_mismatch')
    pinned_up = ['--no-build', '--pull', 'never'] if source == 'release' else []
    pinned_run = ['--pull', 'never'] if source == 'release' else []
    stage('start_fresh_database', ['up', '-d', *pinned_up, '--wait', '--wait-timeout', '180', 'db'])
    stage('initialize_owner_store',
          ['run', '--rm', '--no-deps', '-T', *pinned_run, 'backend', 'python', 'ops/self_hosted_owner.py', 'initialize'])
    if api_key is not None:
        run(prefix + ['run', '--rm', '--no-deps', '-T', *pinned_run, 'backend', 'python', 'ops/self_hosted_owner.py',
                      'set-api-key', '--stdin'],
            cwd=directory, input_data=api_key + '\n')
        api_key = None
    stage('start_backend', ['up', '-d', *pinned_up, '--wait', '--wait-timeout', '180', 'backend'])
    address = f'http://127.0.0.1:{port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(address + '/auth/session', timeout=5) as response:
        state = json.load(response)
    if (state.get('configured') is not False or state.get('authenticated') is not False
            or state.get('username') is not None or state.get('access_mode') != 'pending'):
        raise InstallError('unexpected_owner_state')
    # Issue last so the code does not expire during image build or DB startup.
    print_address(port)
    sys.stdout.flush()
    write_private(directory / 'installed.json', json.dumps({
        'status': 'owner_setup_ready', 'project': project, 'port': port,
        'database_id': backend['LIC_DATABASE_ID'],
        'version': version, 'source': source,
        'collection_started': False}).encode())
    print('Expired or lost code? Run: sudo python3 ' + str(directory / 'manage.py') + ' setup-code', flush=True)
    print('Change the YouTube key: sudo python3 ' + str(directory / 'manage.py') + ' set-key', flush=True)
    print('Advanced settings: ' + str(directory / 'config/config.ini'), flush=True)
    run(prefix + ['exec', 'backend', 'python', 'ops/self_hosted_owner.py', 'setup-code'],
        cwd=directory, interactive=True)
    print('Installation complete. Enter the code and choose account or local access at ' + address + '/auth',
          flush=True)
    return {'status': 'owner_setup_ready', 'address': address + '/auth', 'project': project,
            'version': version, 'source': source,
            'provider_requests': 0, 'collection_started': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--directory', type=Path, help='New absolute folder; otherwise ask (default /opt/leti-cook).')
    parser.add_argument('--port', type=int, help='Explicit application port; otherwise suggest a free port from 8002.')
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args()
    try:
        if args.port is not None:
            validate_port(args.port)
        _, files = verify_bundle(args.bundle)
        if not args.install:
            result = {'status': 'plan', 'fresh_directory': str(args.directory or DEFAULT_DIRECTORY),
                      'port': args.port, 'port_selection': 'explicit' if args.port else 'suggest_free_on_install',
                      'local_only': True, 'changes': False}
        else:
            if not sys.stdin.isatty():
                raise InstallError('interactive_terminal_required_for_setup_code')
            info = check_docker()
            print('Docker checks passed.')
            check_time_sync()
            print('The system clock is synchronised.')
            print('Choose where to install Leti Cook and which port to use.')
            print('Use an absolute folder path without spaces; its parent folder must exist.')
            plan = choose_plan(args)
            print_plan(plan, info, sum(map(len, files.values())))
            os.umask(0o077)
            key = hidden_api_key()
            try:
                result = install(args.bundle, plan['directory'], plan['port'], api_key=key)
            finally:
                key = None
        print(json.dumps(result))
        return 0
    except (EOFError, KeyboardInterrupt):
        print('Stopped by user. Any partial installation is retained.', file=sys.stderr)
        return 130
    except (InstallError, OSError, ValueError, subprocess.SubprocessError) as error:
        reason = (str(error) if isinstance(error, InstallError) and re.fullmatch(r'[a-z0-9_]+', str(error))
                  else type(error).__name__)
        print('reason=' + reason, file=sys.stderr)
        if reason in REASON_HINTS:
            print(REASON_HINTS[reason], file=sys.stderr)
        print('Installation stopped. Existing data is never removed or reused; any new partial installation is retained. '
              'Check the last stage and prerequisites before retrying with a different empty directory.',
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
