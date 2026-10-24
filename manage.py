"""Local installation commands. Secrets are entered/displayed only on the TTY."""
import argparse, json, re, subprocess, sys
from pathlib import Path
# Host entrypoints must not add unlisted bytecode to the checksummed release.
sys.dont_write_bytecode = True
try:
    from install import safe_path, check_docker, compose_prefix, command, InstallError
except ModuleNotFoundError:
    from ops.install_self_hosted import safe_path, check_docker, compose_prefix, command, InstallError
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
# Columns where older versions stored provider error text, which could carry
# the key from a request URL. Each holds a code in this version, so a match is
# replaced by a code, never cleared: the row keeps its error state.
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
SCRIPTABLE_ACTIONS = ('check-secrets', 'clear-sign-in-limit', 'check-settings', 'restart', 'purge-update-copy', 'update')


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
}
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
    check_docker()
    prefix = compose_prefix(directory, project)
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
            command(prefix + ['exec', 'backend', 'python', 'ops/self_hosted_owner.py', 'setup-code'],
                    cwd=directory, interactive=True)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='store_true', help='Print the installed product version and exit.')
    parser.add_argument('action', nargs='?', choices=('setup-code', 'set-key', 'restart', 'check-secrets', 'redact-secrets',
                                           'clear-sign-in-limit', 'check-settings', 'purge-update-copy', 'update', 'restore-update-copy', 'uninstall'))
    parser.add_argument('--package', type=Path, help='Signed release archive, with its adjacent metadata and signature.')
    parser.add_argument('--check', action='store_true', help='Verify the available release without updating.')
    parser.add_argument('--pre', action='store_true', help='Allow an explicitly selected prerelease.')
    parser.add_argument('--build-local', action='store_true', help='Build images from the verified package.')
    args = parser.parse_args()
    if not args.version and args.action is None:
        parser.error('the following arguments are required: action')
    if args.version and args.action is not None:
        parser.error('--version cannot be combined with an action')
    if args.action != 'update' and any((args.package, args.check, args.pre, args.build_local)):
        parser.error('update options require the update action')
    update_check_started = False
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
                # Recovery admission completed. A check cannot create a journal
                # or start an update; failures before this point may be recovery.
                update_check_started = args.check
                return update(layout, args.package, check=args.check, pre=args.pre, build_local=args.build_local)
            return _dispatch(args, directory, project)
    except InstallError as error:
        if str(error) == 'interactive_terminal_required':
            print(f'{args.action} needs an interactive terminal. Run it in a terminal; over SSH, use ssh -t.',
                  file=sys.stderr)
        else:
            print('Command failed. Run in the installed directory with Docker access.', file=sys.stderr)
        return 1
    except ValueError as error:
        code = str(error)
        if re.fullmatch(r'(?:uninstall|update|release|schema)_[a-z0-9_]{1,100}', code):
            journal = ('The saved removal journal records the remaining work.' if code.startswith('uninstall_')
                       else 'No update was started.' if update_check_started
                       else 'The update journal records any recovery still needed.')
            print('Command stopped: ' + code + '. ' + UPDATE_STOP_HINTS.get(code, journal), file=sys.stderr)
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
