"""Initialization closes SQLite on success and failure; no network or PostgreSQL."""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from db.self_hosted_auth import OwnerAuthRepository


class InitializationConnectionTests(unittest.TestCase):
    def initialize(self, path, value):
        connections = []
        connect = sqlite3.connect
        def tracked(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connections.append(connection)
            return connection
        with patch('db.self_hosted_auth.sqlite3.connect', side_effect=tracked):
            try:
                result = OwnerAuthRepository.initialize(path, value, 1800000000)
            finally:
                self.assertEqual(len(connections), 1)
                with self.assertRaises(sqlite3.ProgrammingError):
                    connections[0].execute('SELECT 1')
        return result

    def test_success_commits_owner_and_closes_initial_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'auth.sqlite3'
            repository = self.initialize(path, 'dummy')
            self.assertFalse(repository.configured())
            with repository.transaction() as connection:
                self.assertEqual(connection.execute('SELECT password_hash FROM owner').fetchone()[0], 'dummy')

    def test_failure_retains_file_but_closes_initial_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'auth.sqlite3'
            with self.assertRaises(sqlite3.IntegrityError):
                self.initialize(path, None)
            self.assertTrue(path.is_file())
            with self.assertRaises(FileExistsError):
                OwnerAuthRepository.initialize(path, 'dummy', 1800000000)


if __name__ == '__main__':
    unittest.main()
