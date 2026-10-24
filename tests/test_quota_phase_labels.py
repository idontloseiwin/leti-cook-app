"""Quota attempts keep the phase that spent them (tester findings T-09, T-28). Source only: no server or database."""
import ast
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from operational_copy_policy import QUOTA_LABELS, quota_label  # noqa: E402
from typed_playlist import VIDEO_TYPES  # noqa: E402

SCHEMA = (ROOT / 'schema/schema.sql').read_text(encoding='utf-8')

# Values bound to a phase or reason name that never reach youtube_api_get. A new value fails the test
# until it gets a label or lands here with its reason.
NOT_SENT = {
    'choosing', 'complete', 'error', 'needs_key', 'preparing', 'ready', 'saved',  # setup screen states
    'preflight',  # database readiness telemetry
    'hydrate_videos', 'initial_stats',  # initial job summary steps
    'target_types',  # initial-job planning response reason; reconciles targets without a provider dispatch
    'channel_hidden', 'inventory_completed', 'inventory_not_completed', 'inventory_not_ready',  # decisions and stops
    'inventory_sources_incomplete', 'inventory_stale', 'no_long_playlist', 'source_stale',
    'database_transport_error', 'database_video_collection_disabled', 'schema_not_ready', 'worker_unhandled_error',
    'bound', 'prepared',  # db/jobs/operator_job_inventory_chain{_proof,s}: checkpoint proof phase
    'candidate', 'claiming', 'claimed',  # collection/inventory_feeder + auto_pass: private RAM records
    'unavailable', 'connection_failed', 'pool_closed',  # db/base/postgres: DB exception reasons
    'playlist_not_found',  # channel_explorer: absent playlist response, no provider dispatch
    # Passive pipeline events name a local state/reason, not a provider quota phase.
    'worker_failed', 'backend_shutdown', 'handoff_timeout', 'terminate_timeout',
    'configuration', 'handoff_requested', 'maintenance', 'runtime_initialization',
    'setup_or_collection_disabled', 'shared_lock_busy',  # worker lifecycle/admission
    'transport_error', 'persistence_unconfirmed', 'required_counter_fallback',
    'pass_stopped_or_budget_exhausted', 'scheduled',  # provider observation/collector decisions
    'already_owned', 'coordinator_stopped', 'ready_full', 'reconciliation_required',  # dispatcher
    'await_receipt', 'failure_settlement_unknown', 'page_budget_exhausted', 'settlement_unknown',
    'finish_unconfirmed', 'stale_running', 'start_unconfirmed',  # inventory/run receipt state
    'inventory_queue_owned',  # dispatcher owns discovery; not a provider phase
    'items_invalid', 'provider_server_error',  # bounded provider diagnostics, not quota labels
    # UI reads observe cache/DB/HTTP work without spending YouTube quota.
    'cache', 'cache_wait', 'component', 'loader', 'scan', 'http',
    'db_execution', 'db_pool_wait', 'db_transaction',
    'cache_invalidated', 'cache_wait_timeout', 'cancelled', 'component_unavailable',
    'database_error', 'invalid_response_envelope', 'source_expired', 'unexpected_error',
}


def bound_values(tree):
    """Strings given to a phase or reason: call keywords, parameter defaults and dict entries."""
    values, prefixes = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            pairs = [(keyword.arg, keyword.value) for keyword in node.keywords]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            positional = node.args.posonlyargs + node.args.args
            pairs = list(zip([a.arg for a in positional[len(positional) - len(node.args.defaults):]], node.args.defaults))
            pairs += [(a.arg, d) for a, d in zip(node.args.kwonlyargs, node.args.kw_defaults) if d is not None]
        elif isinstance(node, ast.Dict):
            pairs = [(key.value, value) for key, value in zip(node.keys, node.values) if isinstance(key, ast.Constant)]
        else:
            continue
        for name, value in pairs:
            if name not in ('phase', 'reason'):
                continue
            if isinstance(value, ast.BinOp) and isinstance(value.left, ast.Constant):
                prefixes.add(value.left.value)      # "typed_inventory_" + video type
            elif isinstance(value, ast.Constant) and isinstance(value.value, str) \
                    and re.fullmatch(r'[a-z][a-z0-9_]*', value.value):
                values.add(value.value)
    return values, prefixes


class QuotaPhaseLabelTests(unittest.TestCase):
    def test_planning_response_reason_does_not_expand_the_quota_phase_vocabulary(self):
        # This intentionally broad source scan also sees domain response reasons.
        # Keep the exclusion explicit rather than silently widening quota labels.
        values, prefixes = bound_values(ast.parse("dict(action='targets_reconciled', reason='target_types')"))
        self.assertEqual(values, {'target_types'})
        self.assertEqual(prefixes, set())
        self.assertIn('target_types', NOT_SENT)
        self.assertNotIn('target_types', QUOTA_LABELS['phase'])
        self.assertEqual(quota_label('phase', 'target_types'), 'other')

    def test_internal_checkpoint_and_db_reasons_do_not_expand_provider_labels(self):
        values={'bound','prepared','candidate','claiming','claimed','unavailable',
                'connection_failed','pool_closed','playlist_not_found',
                'already_owned','coordinator_stopped','ready_full','reconciliation_required',
                'required_counter_fallback','runtime_initialization','scheduled',
                'cache','cache_wait','component','loader','scan','http',
                'db_execution','db_pool_wait','db_transaction','cache_invalidated',
                'cache_wait_timeout','cancelled','component_unavailable','database_error',
                'invalid_response_envelope','source_expired','unexpected_error',
                'inventory_queue_owned','items_invalid','provider_server_error'}
        self.assertLessEqual(values,NOT_SENT)
        for value in values:
            self.assertNotIn(value,QUOTA_LABELS['phase'])
            self.assertEqual(quota_label('phase',value),'other')

    def test_unrecognized_provider_phase_is_still_detected_and_not_exempted(self):
        values, prefixes = bound_values(ast.parse("youtube_api_get(phase='new_provider_phase')"))
        self.assertEqual(values, {'new_provider_phase'})
        self.assertEqual(prefixes, set())
        self.assertTrue(values.isdisjoint(NOT_SENT))
        self.assertNotEqual(quota_label('phase', 'new_provider_phase'), 'new_provider_phase')

    def test_phases_the_collector_sends_are_kept(self):
        sent, prefixes = set(), set()
        for path in (ROOT / 'application').rglob('*.py'):
            if 'tests' not in path.parts:
                values, found = bound_values(ast.parse(path.read_text(encoding='utf-8')))
                sent, prefixes = sent | values, prefixes | found
        self.assertEqual(prefixes, {'typed_inventory_'})
        sent |= {'typed_inventory_' + video_type for video_type in VIDEO_TYPES}
        # The phases the live test saw as "other" (T-28) must be found, or the scan went blind.
        self.assertLessEqual({'initial_channel_hydration', 'typed_inventory_long', 'initial_28d_import'}, sent)
        self.assertLessEqual(NOT_SENT, sent, 'drop values the code no longer uses')
        for phase in sorted(sent - NOT_SENT):
            self.assertEqual(quota_label('phase', phase), phase, phase)


if __name__ == '__main__':
    unittest.main()
