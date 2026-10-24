"""Current native fixture forwards an explicit process snapshot without fallback."""
import unittest
from unittest.mock import Mock, patch
from collection_schedule import CollectionSchedules
from schema25_regressions import fixtures


class ScheduleFixtureTests(unittest.TestCase):
    def capture(self, **kwargs):
        admin, database = Mock(), Mock()
        stop = RuntimeError("stop_before_native_setup")
        with patch.object(fixtures.psycopg, "connect", return_value=admin), \
                patch.object(fixtures, "PostgresDatabase", return_value=database) as factory, \
                patch.object(fixtures.Fixture, "rows", side_effect=stop):
            with self.assertRaisesRegex(RuntimeError, "stop_before_native_setup"):
                fixtures.Fixture("synthetic", "postgresql://authenticator:synthetic@127.0.0.1:5432/leti-cook", object(), **kwargs)
        admin.close.assert_called_once_with()
        database.close.assert_called_once_with()
        return factory.call_args.kwargs["cadence_snapshot"]

    def test_omitted_model_is_explicit_synthetic_default(self):
        self.assertEqual(self.capture(), CollectionSchedules().cadence_snapshot())

    def test_explicit_snapshot_is_forwarded_by_identity(self):
        snapshot = CollectionSchedules().cadence_snapshot()
        self.assertIs(self.capture(cadence_snapshot=snapshot), snapshot)

    def test_explicit_absence_is_not_defaulted(self):
        self.assertIsNone(self.capture(cadence_snapshot=None))
