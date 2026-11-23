"""Shared SQL state transition; native PG constraint/concurrency proof is separate."""
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from db.user_work.user_work_restore import REBASE_PENDING_PARENT_SQL
from db.retention import deleted_channel_retention


class PendingParentRebaseTests(unittest.TestCase):
    def transition(self, state, *, authorized_revision=7):
        with sqlite3.connect(':memory:') as db:
            db.row_factory = sqlite3.Row
            db.execute('''CREATE TABLE pending (
                revision INTEGER, reconnect_revision INTEGER, reconnect_state TEXT,
                parent_ref TEXT, user_input TEXT, reconnect_lease_token TEXT,
                reconnect_lease_expires_at TEXT, reconnect_not_before TEXT,
                reconnect_requested_at TEXT, reconnect_attempt_count INTEGER,
                reconnect_error_code TEXT)''')
            db.execute('INSERT INTO pending VALUES (?,?,?,?,?,?,?,?,?,?,?)', (
                7, None if state == 'idle' else authorized_revision, state,
                'parent', 'unchanged-locator', 'old-token' if state == 'running' else None,
                'future-expiry' if state == 'running' else None,
                'future-retry' if state == 'waiting' else None,
                None if state == 'idle' else 'original-request',
                0 if state == 'idle' else 3,
                'provider_key_rejected' if state == 'blocked' else None,
            ))
            before = dict(db.execute('SELECT * FROM pending').fetchone())
            db.execute('UPDATE pending SET parent_ref=NULL,' + REBASE_PENDING_PARENT_SQL)
            after = dict(db.execute('SELECT * FROM pending').fetchone())
            return before, after

    def test_all_states_keep_intent_and_wait_but_invalidate_inflight_response(self):
        for state in ('idle', 'queued', 'running', 'waiting', 'blocked'):
            with self.subTest(state=state):
                before, after = self.transition(state)
                self.assertEqual(after['revision'], 8)
                self.assertEqual(after['reconnect_revision'], None if state == 'idle' else 8)
                self.assertEqual(after['reconnect_state'], 'queued' if state == 'running' else state)
                self.assertIsNone(after['parent_ref'])
                self.assertIsNone(after['reconnect_lease_token'])
                self.assertIsNone(after['reconnect_lease_expires_at'])
                for field in ('user_input', 'reconnect_not_before', 'reconnect_requested_at',
                              'reconnect_attempt_count', 'reconnect_error_code'):
                    self.assertEqual(after[field], before[field])

    def test_stale_authorization_never_authorizes_new_revision(self):
        # Such a row is refused by the new PG CHECK. The transition must not
        # silently repair it by inventing authorization for the current input.
        _, after = self.transition('queued', authorized_revision=6)
        self.assertEqual(after['revision'], 8)
        self.assertEqual(after['reconnect_revision'], 6)

    def test_both_cleanup_paths_use_the_same_transition(self):
        self.assertIn(REBASE_PENDING_PARENT_SQL, deleted_channel_retention._PARENT_PENDING)
        class Transaction:
            def __init__(self): self.statements = []
            def fetch_one(self, sql, params): return {'id': 'parent'}
            def fetch_all(self, sql, params, *, max_rows): return []
            def execute(self, sql, params): self.statements.append(sql); return 0
        tx = Transaction()
        deleted_channel_retention._pending(tx, {'owner': 'parent'})
        statements = [sql for sql in tx.statements if sql.startswith('UPDATE public.user_channel_restore_items')]
        self.assertEqual(len(statements), 1)
        self.assertIn(REBASE_PENDING_PARENT_SQL, statements[0])


if __name__ == '__main__':
    unittest.main()
