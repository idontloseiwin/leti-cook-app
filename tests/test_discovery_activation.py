"""Activation-only grace and bounded diagnostics; no database/provider calls."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from collection_schedule import discovery_schedules
from db.base.postgres import DatabaseServerError
from db.discovery_activation import _parameters,activate,prepare,readiness,PreparationContract
from discovery_policy import DiscoverySettings


class ActivationConfigurationTests(unittest.TestCase):
    def test_full_policy_keeps_custom_grace_separate_from_inventory_and_missing_periods(self):
        tx=Mock(cadence_snapshot=discovery_schedules().cadence_snapshot())
        policy=replace(DiscoverySettings(),activation_grace_seconds=93717,
            retry_first_seconds=30,retry_second_seconds=60,retry_third_seconds=120,
            retry_sparse_seconds=600,review_retry_seconds=3600)
        parameters=_parameters(tx,['long','short'],policy)
        self.assertEqual(parameters['grace'],93717)
        self.assertEqual(json.loads(parameters['full_seconds']),{'long':604800,'short':604800})
        self.assertEqual(json.loads(parameters['missing_seconds']),{'long':86400,'short':86400})
        self.assertEqual(parameters['hours'],696)

    def test_missing_or_partial_policy_is_rejected_before_database_work(self):
        database=Mock();host=PreparationContract(True,True,True,True,True)
        for policy in (None,{'activation_grace_seconds':172800},True):
            for callback in (lambda:readiness(database,settings=policy),
                             lambda:prepare(database,prepared_host=host,settings=policy),
                             lambda:activate(database,prepared_host=host,settings=policy)):
                with self.subTest(policy=policy),self.assertRaisesRegex(DatabaseServerError,'discovery_settings_required'):
                    callback()
        database.transaction.assert_not_called()

    def test_blocker_sample_limits_do_not_accept_bool_or_unbounded_input(self):
        database=Mock()
        for limit in (True,0,101,1.0,'10'):
            with self.subTest(limit=limit),self.assertRaisesRegex(DatabaseServerError,'discovery_blocker_limit_invalid'):
                readiness(database,settings=DiscoverySettings(),blocker_limit=limit)
        database.transaction.assert_not_called()


if __name__=='__main__':unittest.main()
