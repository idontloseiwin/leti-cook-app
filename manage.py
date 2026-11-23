"""Local installation commands. Secrets are entered/displayed only on the TTY."""
import argparse, json, os, re, subprocess, sys
from pathlib import Path
# Host entrypoints must not add unlisted bytecode to the checksummed release.
sys.dont_write_bytecode = True
from install import safe_path, check_docker, compose_prefix, command, InstallError
from application.release_version import read_version

# A YouTube Data API key, or a URL carrying a key parameter. The same pattern is
# valid as a PostgreSQL POSIX regex and as a Python regex.
SECRET_PATTERN = r'AIza[0-9A-Za-z_-]{35}|[?&]key='
SCAN_SCHEMAS = ('public', 'operator_private')
SCAN_TYPES = ('text', 'varchar', 'bpchar', 'json', 'jsonb', '_text', '_varchar')
FINDINGS_PER_COLUMN = 20
HIDDEN_ROW_KEY = '(row key hidden)'
# The database cancels a slow statement or lock wait itself; the client
# timeout only guards against a stuck docker/psql process.
SCAN_STATEMENT_TIMEOUT_SECONDS = 1800
SCAN_LOCK_TIMEOUT_SECONDS = 5
SCAN_CLIENT_TIMEOUT_SECONDS = SCAN_STATEMENT_TIMEOUT_SECONDS + 120
SCAN_SESSION_OPTIONS = ('-c default_transaction_read_only=on'
                        f' -c statement_timeout={SCAN_STATEMENT_TIMEOUT_SECONDS * 1000}'
                        f' -c lock_timeout={SCAN_LOCK_TIMEOUT_SECONDS * 1000}')
EXIT_FOUND = 3
EXIT_INCOMPLETE = 4
EXIT_CANCELLED = 2
# These columns may contain provider errors with a key in a request URL.
# Replace a matching message with its error code to preserve the row's state.
REDACT_COLUMNS = (('channels', 'channel_metadata_error'), ('channels', 'deep_inventory_error'),
                  ('channels', 'deep_bypass_reason'), ('channels', 'channel_discovery_error'),
                  ('channel_playlist_state', 'discovery_error'))
REDACTED_CODE = 'redacted_secret'
REDACT_SESSION_OPTIONS = (f'-c statement_timeout={SCAN_STATEMENT_TIMEOUT_SECONDS * 1000}'
                          f' -c lock_timeout={SCAN_LOCK_TIMEOUT_SECONDS * 1000}')


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def sql_identifier(name):
    return '"' + str(name).replace('"', '""') + '"'


COLUMNS_SQL = (
    'SELECT c.table_schema, c.table_name, c.column_name, coalesce(('
    'SELECT json_agg(k.column_name ORDER BY k.ordinal_position) '
    'FROM information_schema.table_constraints t JOIN information_schema.key_column_usage k '
    'ON k.constraint_schema = t.constraint_schema AND k.constraint_name = t.constraint_name '
    'AND k.table_schema = t.table_schema AND k.table_name = t.table_name '
    "WHERE t.constraint_type = 'PRIMARY KEY' AND t.table_schema = c.table_schema AND t.table_name = c.table_name"
    "), '[]') "
    'FROM information_schema.columns c JOIN information_schema.tables b '
    "ON b.table_schema = c.table_schema AND b.table_name = c.table_name AND b.table_type = 'BASE TABLE' "
    'WHERE c.table_schema IN (' + ', '.join(map(sql_literal, SCAN_SCHEMAS)) + ') '
    'AND c.udt_name IN (' + ', '.join(map(sql_literal, SCAN_TYPES)) + ') '
    'ORDER BY 1, 2, 3')


def scan_sql(columns):
    """One read-only query returning (schema, table, column, row key) for matching values only."""
    queries = []
    for schema, table, column, key in columns:
        if key and column not in key:
            row_key = 'concat_ws(' + sql_literal(',') + ', ' + ', '.join(sql_identifier(k) + '::text' for k in key) + ')'
        else:
            row_key = sql_literal(HIDDEN_ROW_KEY if key else '(no primary key)')
        queries.append(
            '(SELECT ' + ', '.join(map(sql_literal, (schema, table, column))) + ', ' + row_key
            + ' FROM ' + sql_identifier(schema) + '.' + sql_identifier(table)
            + ' WHERE ' + sql_identifier(column) + '::text ~ ' + sql_literal(SECRET_PATTERN)
            + ' LIMIT ' + str(FINDINGS_PER_COLUMN + 1) + ')')
    return '\nUNION ALL\n'.join(queries)


def parse_rows(output, fields):
    rows = []
    for line in output.splitlines():
        if not line:
            continue
        values = line.split('\t')
        if len(values) != fields:
            raise InstallError('unexpected_database_output')
        rows.append(values)
    return rows


def check_secrets(directory, prefix, *, run=command, emit=print):
    """Report where YouTube API keys may be stored; never print the values."""
    psql = prefix + ['exec', '-T', '-e', 'PGOPTIONS=' + SCAN_SESSION_OPTIONS, 'db',
                     'psql', '--no-psqlrc', '--quiet', '--no-align', '--tuples-only', '--field-separator', '\t',
                     '--set', 'ON_ERROR_STOP=1', '--username', 'postgres', '--dbname', 'leti-cook', '--command']
    try:
        columns = [(schema, table, column, json.loads(key))
                   for schema, table, column, key in parse_rows(run(psql + [COLUMNS_SQL], cwd=directory, timeout=120), 4)]
        if not columns:
            raise InstallError('no_text_columns_found')
        findings = parse_rows(run(psql + [scan_sql(columns)], cwd=directory,
                                  timeout=SCAN_CLIENT_TIMEOUT_SECONDS), 4)
    except (subprocess.TimeoutExpired, InstallError, ValueError) as error:
        # A timed-out, cancelled or failed scan read only part of the database:
        # never report it as clean. The reason is a fixed code, not database text.
        reason = 'timeout' if isinstance(error, subprocess.TimeoutExpired) else (
            str(error) if isinstance(error, InstallError) else 'unexpected_database_output')
        emit(f'Stored secret check did not finish ({reason}). The database was not fully checked; '
             'no result is available.')
        return EXIT_INCOMPLETE
    if not findings:
        emit(f'Stored secret check: no YouTube API key found in {len(columns)} text columns.')
        return 0
    locations = {}
    for schema, table, column, row_key in findings:
        # A key column can itself hold a match; never print it.
        locations.setdefault(f'{schema}.{table}.{column}', []).append(
            HIDDEN_ROW_KEY if re.search(SECRET_PATTERN, row_key) else row_key)
    emit('Stored secret check: possible YouTube API keys found. Values are not shown.')
    for location, row_keys in locations.items():
        count = f'{FINDINGS_PER_COLUMN}+' if len(row_keys) > FINDINGS_PER_COLUMN else str(len(row_keys))
        emit(f'  {location}: {count} rows; row keys: ' + ', '.join(row_keys[:FINDINGS_PER_COLUMN]))
    emit('This command only reports. Run manage.py redact-secrets to replace keys in stored error columns; '
         'do not delete other rows by hand, they may hold user work. Revoke the old key in Google Cloud '
         'and create a new one.')
    return EXIT_FOUND


def redact_columns_sql():
    pairs = ' OR '.join(f'(table_name = {sql_literal(t)} AND column_name = {sql_literal(c)})' for t, c in REDACT_COLUMNS)
    return ("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' AND ("
            + pairs + ') ORDER BY 1, 2')


def secret_match(column):
    return sql_identifier(column) + '::text ~ ' + sql_literal(SECRET_PATTERN)


def redact_preview_sql(columns):
    return '\nUNION ALL\n'.join(
        f'SELECT {sql_literal(table)}, {sql_literal(column)}, count(*) FROM public.{sql_identifier(table)} '
        f'WHERE {secret_match(column)}' for table, column in columns)


def redact_apply_sql(columns):
    # One statement per column; the caller runs them in one transaction.
    return ''.join(
        f'WITH changed AS (UPDATE public.{sql_identifier(table)} SET {sql_identifier(column)} = '
        f'{sql_literal(REDACTED_CODE)} WHERE {secret_match(column)} RETURNING 1) '
        f'SELECT {sql_literal(table)}, {sql_literal(column)}, count(*) FROM changed;\n' for table, column in columns)


def redact_secrets(directory, prefix, *, run=command, ask=input, emit=print):
    """Replace keys in stored error columns with a code, then check the whole database."""
    psql = prefix + ['exec', '-T', '-e', 'PGOPTIONS=' + REDACT_SESSION_OPTIONS, 'db',
                     'psql', '--no-psqlrc', '--quiet', '--no-align', '--tuples-only', '--field-separator', '\t',
                     '--set', 'ON_ERROR_STOP=1', '--username', 'postgres', '--dbname', 'leti-cook']
    try:
        columns = [tuple(row) for row in parse_rows(run(psql + ['--command', redact_columns_sql()],
                                                        cwd=directory, timeout=120), 2)]
        found = [(t, c, int(n)) for t, c, n in parse_rows(
            run(psql + ['--command', redact_preview_sql(columns)], cwd=directory,
                timeout=SCAN_CLIENT_TIMEOUT_SECONDS), 3) if int(n)] if columns else []
    except (subprocess.TimeoutExpired, InstallError, ValueError):
        emit('Stored error columns could not be read. Nothing was changed.')
        return EXIT_INCOMPLETE
    if not found:
        emit('Stored error columns hold no YouTube API key. Nothing to replace.')
    else:
        for table, column, count in found:
            emit(f'  public.{table}.{column}: {count} rows')
        answer = ask(f'Type REDACT to replace these values with the code {REDACTED_CODE}: ')
        if answer.strip() != 'REDACT':
            emit('Nothing was changed.')
            return EXIT_CANCELLED
        try:
            changed = parse_rows(run(psql + ['--single-transaction', '--file', '-'], cwd=directory,
                                     input_data=redact_apply_sql([(t, c) for t, c, _ in found]),
                                     timeout=SCAN_CLIENT_TIMEOUT_SECONDS), 3)
        except (subprocess.TimeoutExpired, InstallError, ValueError):
            emit('Replacing the values failed; the transaction was rolled back. Nothing was changed.')
            return EXIT_INCOMPLETE
        emit(f'Replaced {sum(int(n) for _, _, n in changed)} values. Checking the whole database again:')
    return check_secrets(directory, prefix, run=run, emit=emit)


# A settings value the backend refuses would leave it restarting in a loop, so
# check-settings validates both files; restart then waits for backend health.
SETTINGS_CHECK = ['run', '--rm', '--no-deps', '-T', 'backend', 'python', 'ops/self_hosted_container.py',
                  'check-settings']
RESTART_WAIT_SECONDS = 180
PROBLEM_LINE = re.compile(r'[ -~]{1,200}')


def check_settings(directory, prefix):
    checked = subprocess.run(prefix + SETTINGS_CHECK, cwd=directory, stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=120)
    if checked.returncode:
        lines = [line for line in (checked.stdout or '').splitlines() if PROBLEM_LINE.fullmatch(line)]
        print('Collection settings were not applied: ' + (lines[-1] if lines else 'it could not be checked.'),
              file=sys.stderr)
        return 1
    return 0


def restart(directory, prefix):
    if check_settings(directory, prefix):
        print('The application was not restarted. Fix the file and run restart again.', file=sys.stderr)
        return 1
    command(prefix + ['restart', 'backend'], cwd=directory)
    try:
        command(prefix + ['up', '-d', '--wait', '--wait-timeout', str(RESTART_WAIT_SECONDS), '--no-deps',
                          '--no-recreate', 'backend'], cwd=directory, timeout=RESTART_WAIT_SECONDS + 60)
    except InstallError:
        print(f'The application did not become ready within {RESTART_WAIT_SECONDS} seconds after the restart.',
              file=sys.stderr)
        return 1
    print('Application restarted. Database was not restarted.')
    return 0


# These ask nothing and print no secret, so they may run from scripts and cron.
SCRIPTABLE_ACTIONS = ('check-secrets', 'clear-sign-in-limit', 'check-settings', 'restart', 'purge-update-copy', 'update', 'agent-key-list', 'agent-key-revoke')


def purge_update_copy(directory, state):
    """Expiry cleanup remains available without Docker or a working database."""
    from ops.update_copy_store import load_layout, purge_copy
    try:
        layout = load_layout(directory, project=state['project'], database_id=state.get('database_id'))
        removed = purge_copy(layout)
    except (OSError, ValueError, subprocess.SubprocessError):
        print('The temporary update copy could not be cleaned. Check its cleanup timer before continuing.', file=sys.stderr)
        return 1
    print('Expired update copy removed.' if removed else 'No expired update copy to remove.')
    return 0


# set-key asks YouTube about the new key once the backend is up, so setup never
# starts preparing with a key that cannot work, and a running installation never
# swaps its working key for one YouTube refuses.
KEY_CHECK = ['exec', '-T', 'backend', 'python', 'ops/self_hosted_owner.py', 'check-api-key']
ACCESS_HELP = "Enable YouTube Data API v3 for the key's project, allow this server in the key's restrictions, "
# Plain explanations for update refusals at the source-age check. The current
# version keeps running; these conditions clear on their own or by waiting.
UPDATE_STOP_HINTS = {
    'update_copy_window_unconfirmed': 'Some stored YouTube data has no confirmed acquisition time yet. '
        'This is usual while a large load runs or after the daily quota ran out. '
        'The current version keeps running; run the update again when loading has finished and the quota has reset.',
    'update_copy_window_expired': 'YouTube data older than 29 days is still waiting for cleanup. '
        'The current version keeps running; leave it on so cleanup catches up, then run the update again.',
    'update_copy_window_unavailable': 'The age of stored YouTube data could not be checked. '
        'The current version keeps running; check that its database is healthy, then run the update again.',
    # The shared lock of updates, recovery and removal; nothing was started.
    'update_lock_busy': 'Another management command, update or removal is running for this installation. '
        'Wait until it finishes, then run the command again.',
    # An unfinished installation: its installer holds the same lock until it writes installed.json.
    'uninstall_installation_in_progress': 'The installer, or another uninstall, is still running for this '
        'folder. Wait until it stops, then run uninstall again.',
}
# The update engine raises these only after it recorded the update in its journal.
ENGINE_STOP_CODES = frozenset({'update_failed_previous_version_recovered',
                               'update_recovery_failed_copy_deadline_still_applies'})


def unfinished_journal(directory, state):
    """'removal' or 'update' when that journal on disk still records unfinished work, else None.

    Read-only. A journal that cannot be read is not claimed."""
    if directory is None or not isinstance(state, dict):
        return None
    try:
        from ops.uninstall_store import JOURNAL_FILENAME
        # A finished removal deletes its journal; any saved one has work left.
        if (Path(str(directory) + '.update') / 'owner' / JOURNAL_FILENAME).exists():
            return 'removal'
        from ops import updater
        pending = updater.journal.load_journal(updater.layout_for(directory, state))
        return 'update' if pending and pending['phase'] not in updater.journal.TERMINAL else None
    except (ImportError, OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def stop_note(action, code, journal):
    if code in UPDATE_STOP_HINTS:
        return UPDATE_STOP_HINTS[code]
    if journal == 'removal':
        return 'The saved removal journal records the remaining work.'
    if journal == 'update':
        return 'The update journal records any recovery still needed.'
    if action == 'update' and code not in ENGINE_STOP_CODES:
        return 'No update was started.'
    return ''


KEY_RESULTS = {
    'key_accepted': (0, 'Key saved and accepted by YouTube. Open /setup in the browser to continue.'),
    'provider_key_rejected': (1, 'YouTube rejected this key, so it was not kept. Check the key and run set-key again.'),
    'provider_access_denied': (1, 'YouTube refused access with this key, so it was not kept. ' + ACCESS_HELP
                                  + 'then run set-key again.'),
    'provider_quota_exceeded': (0, "Key saved. Google reports today's quota as used up, so the key was not checked; "
                                   'setup continues after the quota resets.'),
}
# A running installation keeps its channels, settings and category reference.
RUNNING_KEY_RESULTS = {
    'key_accepted': (0, 'Key accepted by YouTube and now in use. Channels and settings are unchanged.'),
    'provider_key_rejected': (1, 'YouTube rejected this key, so it was not kept; the previous key is still in use. '
                                 'Check the key and run set-key again.'),
    'provider_access_denied': (1, 'YouTube refused access with this key, so it was not kept; the previous key is still '
                                  'in use. ' + ACCESS_HELP + 'then run set-key again.'),
    'provider_quota_exceeded': (0, "Key saved and now in use. Google reports today's quota as used up, so the key was "
                                   'not checked; collection continues after the quota resets.'),
}
RUNNING_KEY_NOT_CHECKED = (0, 'Key saved and now in use, but YouTube could not be asked about it now.')
# The previous key was refused as well: collection stays stopped until a key works.
BOTH_KEYS_REFUSED = (1, 'YouTube refused this key too, so it was not kept. Collection stays stopped until YouTube '
                        'accepts a key. Check the key and its project, then run set-key again.')
KEY_NOT_CHECKED = (0, 'Key saved, but YouTube could not be asked about it now. A new installation checks it when '
                      'preparation starts; a running one keeps its previous key until set-key is run again.')


# Owner helpers in the container exit with this status when they refuse for one
# fixed reason. manage.py names that reason from the status alone; captured
# helper output is never shown.
HELPER_REFUSED_STATUS = 3
DOCKER_ENGINE_UPDATE_HINT = ("Install Docker Engine 28.3.3 or newer from Docker's repository: "
                             'https://docs.docker.com/engine/install/ Then run the update again. No update was started.')
# "Command stopped: <code>." and what to do; the helper may have said why on the terminal.
REFUSAL_HINTS = {
    'owner_access_not_configured': 'Agent keys need finished access setup: open /auth, enter the setup code and '
        'choose account or local access, then run the command again.',
    'owner_already_created': '',
    # Only update refuses an old Docker Engine; the line before says which one was found.
    'docker_engine_28_3_3_required': DOCKER_ENGINE_UPDATE_HINT,
    'docker_engine_version_unknown': DOCKER_ENGINE_UPDATE_HINT,
}


def helper_command(args, refusal, **options):
    try:
        return command(args, **options)
    except InstallError as error:
        if str(error) == 'command_failed_' + str(HELPER_REFUSED_STATUS):
            raise InstallError(refusal) from None
        raise


def check_key(directory, prefix):
    try:
        command(prefix + ['up', '-d', '--wait', '--wait-timeout', '180', '--no-deps', '--no-recreate', 'backend'],
                cwd=directory, timeout=240)
        result = json.loads(command(prefix + KEY_CHECK, cwd=directory, timeout=90).strip().splitlines()[-1])
        status, running = result['status'], result.get('running') is True
    except (InstallError, ValueError, KeyError, IndexError, TypeError, AttributeError, subprocess.TimeoutExpired):
        code, message = KEY_NOT_CHECKED
    else:
        results, not_checked = (RUNNING_KEY_RESULTS, RUNNING_KEY_NOT_CHECKED) if running else (KEY_RESULTS, KEY_NOT_CHECKED)
        code, message = BOTH_KEYS_REFUSED if result.get('previous_key_refused') is True else results.get(status, not_checked)
    print(message, file=sys.stderr if code else sys.stdout)
    return code


def _dispatch(args, directory, project):
    # An old Docker Engine refuses install and update; here it only warns, so the owner can still work.
    check_docker(engine='warn')
    prefix = compose_prefix(directory, project)
    if args.action in ('agent-key-create', 'agent-key-list', 'agent-key-revoke'):
        action = args.action.removeprefix('agent-key-')
        arguments = ['python', '-B', 'ops/self_hosted_agent.py', action]
        if action == 'create':
            arguments += ['--name', args.name, '--days', str(args.days)]
        elif action == 'revoke':
            arguments += ['--key-id', args.key_id]
        interactive = action == 'create'
        output = helper_command(prefix + ['exec', *([] if interactive else ['-T']), 'backend', *arguments],
                                'owner_access_not_configured', cwd=directory, interactive=interactive)
        if output and not interactive:
            print(output.strip())
        return 0
    if args.action == 'check-secrets':
        return check_secrets(directory, prefix)
    if args.action == 'redact-secrets':
        return redact_secrets(directory, prefix)
    if args.action == 'clear-sign-in-limit':
        command(prefix + ['exec', '-T', 'backend', 'python', 'ops/self_hosted_owner.py', 'clear-sign-in-limit'],
                cwd=directory)
        print('Sign-in limit cleared. The password and signed-in browsers are unchanged. '
              'If failed sign-ins continue, find who else can reach the port.')
        return 0
    if args.action == 'check-settings':
        result = check_settings(directory, prefix)
        if result == 0:
            print('Collection settings are valid. The application was not restarted.')
        return result
    if args.action == 'restart':
        return restart(directory, prefix)
    else:
        if args.action == 'setup-code':
            helper_command(prefix + ['exec', 'backend', 'python', 'ops/self_hosted_owner.py', 'setup-code'],
                           'owner_already_created', cwd=directory, interactive=True)
        else:
            # Quiesce the only backend/supervisor before saving a key. No
            # worker may finish preparation with a key being replaced; a
            # running installation keeps its current key until the check.
            command(prefix + ['stop', '--timeout', '30', 'backend'], cwd=directory)
            try:
                command(prefix + ['run', '--rm', '--no-deps', 'backend', 'python', 'ops/self_hosted_owner.py',
                                  'set-api-key'],
                        cwd=directory, interactive=True)
            finally:
                command(prefix + ['start', 'backend'], cwd=directory)
            return check_key(directory, prefix)
    return 0


# An installation that stopped before installed.json keeps only installation.json.
# Its status stays 'preparing' for good (an update copies it as is), so only the
# missing installed.json marks it unfinished; uninstall is its one way out.
UNFINISHED_INSTALLATION = ('This installation did not finish. Remove it with: sudo python3 {} uninstall. '
                           'Then install again into a new folder.')


def unfinished_state(directory):
    """project, port and database_id from installation.json, with the installed.json checks."""
    from ops.bundle_contract import FORMAT
    from ops.update_journal import JOURNAL_FILENAME
    path = safe_path(directory / 'installation.json')
    if path.stat().st_size > 4096:
        raise InstallError('invalid_installation_marker')
    receipt = json.loads(path.read_text())
    if not isinstance(receipt, dict) or receipt.get('format') != FORMAT:
        raise InstallError('invalid_installation_marker')
    state = {name: receipt.get(name) for name in ('project', 'port', 'database_id')}
    if (type(state['project']) is not str or not re.fullmatch(r'leti-cook-[0-9a-f]{12}', state['project'])
            or type(state['port']) is not int or not 1024 <= state['port'] <= 65535
            or type(state['database_id']) is not str or not re.fullmatch(
                r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', state['database_id'])):
        raise InstallError('invalid_installation_project')
    # Only a finished installation can have been updated; never treat it as unfinished.
    if os.path.lexists(Path(str(directory) + '.update') / 'owner' / JOURNAL_FILENAME):
        raise InstallError('invalid_installation_marker')
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='store_true', help='Print the installed product version and exit.')
    parser.add_argument('action', nargs='?', choices=('setup-code', 'set-key', 'restart', 'check-secrets', 'redact-secrets',
                                           'clear-sign-in-limit', 'check-settings', 'purge-update-copy', 'update', 'restore-update-copy', 'uninstall',
                                           'agent-key-create', 'agent-key-list', 'agent-key-revoke'))
    parser.add_argument('--name', help='Name for a new research read-only key.')
    parser.add_argument('--days', type=int, default=None, help='Research key lifetime: 1–365 days, default 30.')
    parser.add_argument('--key-id', help='Identifier of the research key to revoke, not its secret.')
    parser.add_argument('--package', type=Path, help='Signed release archive, with its adjacent metadata and signature.')
    parser.add_argument('--check', action='store_true', help='Verify the available release without updating.')
    parser.add_argument('--pre', action='store_true', help='Allow an explicitly selected prerelease.')
    parser.add_argument('--build-local', action='store_true', help='Build images from the verified package.')
    args = parser.parse_args()
    if args.action == 'agent-key-create':
        args.days = 30 if args.days is None else args.days
        if not args.name or not 1 <= args.days <= 365 or args.key_id:
            parser.error('agent-key-create requires --name and optionally --days (1–365)')
    elif args.action == 'agent-key-revoke':
        if not args.key_id or args.name or args.days is not None:
            parser.error('agent-key-revoke requires --key-id only')
    elif args.name is not None or args.days is not None or args.key_id is not None:
        parser.error('key options require agent-key-create or agent-key-revoke')
    if not args.version and args.action is None:
        parser.error('the following arguments are required: action')
    if args.version and args.action is not None:
        parser.error('--version cannot be combined with an action')
    if args.action != 'update' and any((args.package, args.check, args.pre, args.build_local)):
        parser.error('update options require the update action')
    directory = state = None
    try:
        if args.version:
            directory = Path(__file__).absolute().parent
            if (directory / 'installed.json').exists():
                directory = safe_path(directory)
                state = json.loads((directory / 'installed.json').read_bytes())
                from ops.uninstall import continue_removal
                if continue_removal(directory, state):
                    return 0
                from ops.updater import management
                with management(directory, state) as (_, recovered):
                    if recovered:
                        return 0
            print('Leti Cook ' + read_version())
            return 0
        directory = safe_path(Path(__file__).absolute().parent)
        if args.action not in SCRIPTABLE_ACTIONS and not sys.stdin.isatty():
            raise InstallError('interactive_terminal_required')
        if not os.path.lexists(directory / 'installed.json') and os.path.lexists(directory / 'installation.json'):
            state = unfinished_state(directory)
            if args.action != 'uninstall':
                print(UNFINISHED_INSTALLATION.format(directory / 'manage.py'), file=sys.stderr)
                return 1
            from ops.uninstall import uninstall
            return uninstall(directory, state, partial=True)
        path = safe_path(directory / 'installed.json')
        if path.stat().st_size > 4096:
            raise InstallError('invalid_installation_marker')
        state = json.loads(path.read_text())
        project = state.get('project', '')
        if not re.fullmatch(r'leti-cook-[0-9a-f]{12}', project):
            raise InstallError('invalid_installation_project')
        if args.action == 'purge-update-copy':
            return purge_update_copy(directory, state)
        from ops.uninstall import uninstall, continue_removal
        if args.action == 'uninstall':
            return uninstall(directory, state)
        if continue_removal(directory, state):
            return 0
        from ops.updater import management, update, restore_update_copy, layout_for
        if args.action == 'restore-update-copy':
            from ops.update_journal import update_lock
            layout = layout_for(directory, state)
            with update_lock(layout):
                return restore_update_copy(layout)
        with management(directory, state) as (layout, recovered):
            if recovered:
                print('Recovery completed. Run your command again.')
                return 0
            if args.action == 'update':
                return update(layout, args.package, check=args.check, pre=args.pre, build_local=args.build_local)
            return _dispatch(args, directory, project)
    except InstallError as error:
        if str(error) == 'interactive_terminal_required':
            print(f'{args.action} needs an interactive terminal. Run it in a terminal; over SSH, use ssh -t.',
                  file=sys.stderr)
        elif str(error) in REFUSAL_HINTS:
            hint = REFUSAL_HINTS[str(error)]
            print('Command stopped: ' + str(error) + '.' + (' ' + hint if hint else ''), file=sys.stderr)
        else:
            print('Command failed. Run in the installed directory with Docker access.', file=sys.stderr)
        return 1
    except ValueError as error:
        code = str(error)
        if re.fullmatch(r'(?:uninstall|update|release|schema)_[a-z0-9_]{1,100}', code):
            note = stop_note(args.action, code, unfinished_journal(directory, state))
            print('Command stopped: ' + code + '.' + (' ' + note if note else ''), file=sys.stderr)
        else:
            print('Could not read the product version.' if args.version else
                  'Command failed. Run in the installed directory with Docker access.', file=sys.stderr)
        return 1
    except (OSError, KeyError, KeyboardInterrupt, subprocess.TimeoutExpired):
        message = ('Could not read the product version.' if args.version else
                   'Command failed. Run in the installed directory with Docker access.')
        print(message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
