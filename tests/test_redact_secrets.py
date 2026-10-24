"""manage.py redact-secrets replaces keys only in stored error columns, after confirmation (audit S5-1). No Docker."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import manage  # noqa: E402

KEY = 'AIza' + 'k' * 35
TARGETS = 'channels\tchannel_metadata_error\nchannels\tdeep_inventory_error\n'
PREVIEW = 'channels\tchannel_metadata_error\t2\nchannels\tdeep_inventory_error\t0\n'
APPLIED = 'channels\tchannel_metadata_error\t2\n'
SCAN_COLUMNS = 'public\tchannels\tchannel_metadata_error\t["channel_id"]\n'


class FakeRun:
    """Answers each psql call from its SQL; an exception instance is raised instead."""
    def __init__(self, preview=PREVIEW, applied=APPLIED, findings=''):
        self.preview, self.applied, self.findings = preview, applied, findings
        self.calls, self.inputs = [], []

    def __call__(self, args, *, cwd=None, timeout=300, input_data=None):
        self.calls.append(args)
        sql = input_data if input_data is not None else args[-1]
        if input_data is not None:
            self.inputs.append(input_data)
            answer = self.applied
        elif 'information_schema.columns WHERE table_schema = \'public\' AND (' in sql:
            answer = TARGETS
        elif sql.startswith('SELECT \'channels\''):
            answer = self.preview
        elif sql == manage.COLUMNS_SQL:
            answer = SCAN_COLUMNS
        else:
            answer = self.findings
        if isinstance(answer, BaseException):
            raise answer
        return answer


def redact(run, answer='REDACT'):
    lines, questions = [], []

    def ask(question):
        questions.append(question)
        return answer
    code = manage.redact_secrets(Path('/opt/leti-cook'), ['docker', 'compose'], run=run, ask=ask, emit=lines.append)
    return code, '\n'.join(lines), questions


class RedactSecretsTests(unittest.TestCase):
    def test_found_values_are_replaced_in_one_transaction_then_checked(self):
        run = FakeRun()
        code, output, questions = redact(run)
        self.assertEqual(code, 0)
        self.assertEqual(len(questions), 1)
        self.assertIn('public.channels.channel_metadata_error: 2 rows', output)
        self.assertNotIn('deep_inventory_error', output)
        script, = run.inputs
        self.assertEqual(script.count('UPDATE'), 1)
        self.assertIn('SET "channel_metadata_error" = \'redacted_secret\'', script)
        self.assertIn('--single-transaction', run.calls[2])
        self.assertIn('Replaced 2 values.', output)
        self.assertIn('no YouTube API key found', output)
        self.assertNotIn(KEY, output)

    def test_only_the_listed_error_columns_can_change(self):
        sql = manage.redact_apply_sql(list(manage.REDACT_COLUMNS))
        self.assertEqual(sql.count('UPDATE public.'), len(manage.REDACT_COLUMNS))
        for table, column in manage.REDACT_COLUMNS:
            self.assertIn(f'UPDATE public."{table}" SET "{column}" = \'redacted_secret\' WHERE "{column}"::text ~ ', sql)
        self.assertRegex(manage.REDACTED_CODE, r'^[a-z0-9_]{1,64}$')

    def test_anything_but_redact_changes_nothing(self):
        run = FakeRun()
        code, output, _ = redact(run, answer='yes')
        self.assertEqual(code, manage.EXIT_CANCELLED)
        self.assertEqual(run.inputs, [])
        self.assertIn('Nothing was changed.', output)

    def test_no_key_asks_nothing_and_still_checks_everything(self):
        run = FakeRun(preview='channels\tchannel_metadata_error\t0\n', findings='public\tnotes\tbody\t7\n')
        code, output, questions = redact(run)
        self.assertEqual(code, manage.EXIT_FOUND)
        self.assertEqual(questions, [])
        self.assertEqual(run.inputs, [])
        self.assertIn('public.notes.body', output)

    def test_a_failed_write_reports_that_nothing_changed(self):
        run = FakeRun(applied=manage.InstallError('command_failed_3'))
        code, output, _ = redact(run)
        self.assertEqual(code, manage.EXIT_INCOMPLETE)
        self.assertIn('rolled back', output)
        self.assertNotIn('Replaced', output)

    def test_an_unreadable_database_changes_nothing(self):
        run = FakeRun(preview=manage.InstallError('command_failed_2'))
        code, output, questions = redact(run)
        self.assertEqual(code, manage.EXIT_INCOMPLETE)
        self.assertEqual((questions, run.inputs), ([], []))


if __name__ == '__main__':
    unittest.main()
