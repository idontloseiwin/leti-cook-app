"""The v5 dispatcher uses the exact supervised schedule and interval snapshot."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from collection_schedule import CollectionSchedules,discovery_schedules
from runtime.collection_schedules import schedule_environment
from runtime.discovery import discovery_budget


class CompositionTests(unittest.TestCase):
    def test_v4_has_no_new_budget_and_v5_preserves_interval_and_settings(self):
        for model,expected in ((CollectionSchedules(),False),(discovery_schedules(),True)):
            env={**schedule_environment(model),'LIC_COLLECTION_INTERVAL_MINUTES':'10',
                 'YOUTUBE_QUOTA_PROJECT_ID':'synthetic-project'}
            database=object()
            context=SimpleNamespace(database=database,supervised_environ=lambda:env)
            with patch('db.discovery_budget.DiscoveryBudgetRepository') as repository:
                result=discovery_budget(context)
            if expected:
                repository.assert_called_once_with(database,project_id='synthetic-project',
                    settings=model.discovery,interval_minutes=10)
                self.assertIs(result,repository.return_value)
            else:
                repository.assert_not_called();self.assertIsNone(result)

    def test_broken_or_missing_v5_interval_does_not_invent_allowance(self):
        for value in (None,'oops'):
            env=schedule_environment(discovery_schedules())
            if value is not None:env['LIC_COLLECTION_INTERVAL_MINUTES']=value
            context=SimpleNamespace(database=object(),supervised_environ=lambda:env)
            with patch('db.discovery_budget.DiscoveryBudgetRepository') as repository:
                with self.assertRaises((KeyError,ValueError)):discovery_budget(context)
                repository.assert_not_called()

if __name__=='__main__':unittest.main()
