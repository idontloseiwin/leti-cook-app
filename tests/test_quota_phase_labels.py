"""Quota attempts keep the phase that spent them. Source only: no server or database."""
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

# Durable discovery admission and count settlement return local decisions.
# They are not phases of a provider request and must not expand its vocabulary.
DISCOVERY_LOCAL_REASONS = {
    'campaign_closed', 'existing_grant', 'provider_quota', 'campaign_budget',
    'daily_budget', 'window_budget', 'reserved', 'grant_not_dispatchable',
    'dispatchable', 'grant_already_dispatched', 'grant_expired', 'grant_mismatch',
    'no_demand', 'heads_pending',
    'disabled',  # count settlement returns before work when discovery is inactive
}

# Values bound to a phase or reason name that never reach youtube_api_get. A new value fails the test
# until it gets a label or lands here with its reason.
NOT_SENT = {
    'choosing', 'complete', 'error', 'needs_key', 'preparing', 'ready', 'saved',  # setup screen states
    'full_budget_unavailable',  # manual list check refused locally, no provider dispatch
    'preflight',  # database readiness telemetry
    'hydrate_videos', 'initial_stats',  # initial job summary steps
    'target_types',  # initial-job planning response reason; reconciles targets without a provider dispatch
    'channel_hidden', 'inventory_completed', 'inventory_not_ready',  # decisions and stops
    'no_long_playlist', 'source_stale',
    'database_transport_error', 'database_video_collection_disabled', 'schema_not_ready', 'worker_unhandled_error',
    'bound', 'prepared',  # db/jobs/operator_job_inventory_chain{_proof,s}: checkpoint proof phase
    'candidate', 'claiming', 'claimed',  # collection/inventory_feeder + auto_pass: private RAM records
    'unavailable', 'connection_failed', 'pool_closed',  # db/base/postgres: DB exception reasons
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
    'quota_state_available', 'quota_state_unavailable', 'quota_ledger_unavailable',  # local control/observation diagnostics
    # UI reads observe cache/DB/HTTP work without spending YouTube quota.
    'cache', 'cache_wait', 'component', 'loader', 'http',
    'db_execution', 'db_pool_wait', 'db_transaction',
    'cache_invalidated', 'cache_wait_timeout', 'cancelled', 'component_unavailable',
    'database_error', 'invalid_response_envelope', 'source_expired', 'unexpected_error',
} | DISCOVERY_LOCAL_REASONS


def bound_values(tree, *, diagnostic_reason_calls=()):
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
            # Only explicitly reviewed sinks in exact modules are supplied by
            # the caller. Never exempt phase keywords, other calls or similarly
            # named functions elsewhere. The HTTP-boundary scan is independent.
            if (name == 'reason' and isinstance(node, ast.Call)
                    and ast.unparse(node.func) in diagnostic_reason_calls):
                continue
            if isinstance(value, ast.BinOp) and isinstance(value.left, ast.Constant):
                prefixes.add(value.left.value)      # "typed_inventory_" + video type
            elif isinstance(value, ast.Constant) and isinstance(value.value, str) \
                    and re.fullmatch(r'[a-z][a-z0-9_]*', value.value):
                values.add(value.value)
    return values, prefixes


def direct_provider_phases(tree):
    """Local-reason exemptions never excuse a literal on the HTTP boundary."""
    phases = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else (
            node.func.attr if isinstance(node.func, ast.Attribute) else None)
        if name != 'youtube_api_get':
            continue
        for keyword in node.keywords:
            if keyword.arg == 'phase' and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                phases.add(keyword.value.value)
    return phases


class QuotaPhaseLabelTests(unittest.TestCase):
    def test_closed_journal_reasons_do_not_exempt_provider_phase_keywords(self):
        tree = ast.parse("event('browser', 'rendered', reason='client_report')")
        self.assertEqual(bound_values(tree)[0], {'client_report'})
        self.assertEqual(bound_values(tree, diagnostic_reason_calls={'event'})[0], set())
        for source in ("youtube_api_get(phase='client_report')", "event('browser', 'rendered', phase='client_report')"):
            self.assertEqual(bound_values(ast.parse(source), diagnostic_reason_calls={'event'})[0], {'client_report'})
        self.assertEqual(bound_values(ast.parse("scope('inventory_prepare', reason='head')"), diagnostic_reason_calls={'scope'})[0], set())
        self.assertEqual(bound_values(ast.parse("youtube_api_get(phase='head')"), diagnostic_reason_calls={'scope'})[0], {'head'})
        self.assertEqual(direct_provider_phases(ast.parse("youtube_api_get(phase='client_report')")), {'client_report'})
        self.assertNotIn('client_report', NOT_SENT)
        self.assertEqual(quota_label('phase', 'client_report'), 'other')

    def test_discovery_decisions_stay_local_but_provider_work_keeps_its_phase(self):
        for value in DISCOVERY_LOCAL_REASONS:
            values, prefixes = bound_values(ast.parse(f"dict(allowed=False, reason={value!r})"))
            self.assertEqual(values, {value})
            self.assertEqual(prefixes, set())
            self.assertNotIn(value, QUOTA_LABELS['phase'])
            self.assertEqual(quota_label('phase', value), 'other')
        # Metadata passes the phase positionally, so the keyword scanner below
        # cannot by itself guard these declared discovery provider phases.
        for phase in ('discovery_metadata', 'discovery_membership', 'discovery_hydration'):
            self.assertNotIn(phase, NOT_SENT)
            self.assertEqual(quota_label('phase', phase), phase)

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
                'connection_failed','pool_closed',
                'already_owned','coordinator_stopped','ready_full','reconciliation_required',
                'required_counter_fallback','runtime_initialization','scheduled',
                'cache','cache_wait','component','loader','http',
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

    def test_local_reason_exemption_cannot_hide_a_direct_provider_phase(self):
        local = ast.parse("dict(settled=False, reason='disabled')")
        provider = ast.parse("youtube_api_get(phase='disabled')")
        self.assertEqual(direct_provider_phases(local), set())
        self.assertEqual(direct_provider_phases(provider), {'disabled'})
        self.assertTrue(direct_provider_phases(provider) & NOT_SENT)
        self.assertEqual(quota_label('phase', 'disabled'), 'other')

    def test_phases_the_collector_sends_are_kept(self):
        sent, prefixes, direct = set(), set(), set()
        for path in (ROOT / 'application').rglob('*.py'):
            if 'tests' not in path.parts:
                tree = ast.parse(path.read_text(encoding='utf-8'))
                # event is this module's closed sink; inventory.scope is the
                # imported pipeline_trace scope around prepared head work.
                diagnostics = {
                    ROOT/'application/support_journal.py': {'event'},
                    ROOT/'application/collection/inventory.py': {'scope'},
                    ROOT/'application/db/inventory/inventory_heads.py': {'support_journal.rows'},
                    ROOT/'application/discovery_service.py': {'support_journal.rows'},
                    ROOT/'application/db/websub.py': {'support_journal.event'},
                    ROOT/'application/ops/self_hosted_collect.py': {'support_journal.event'},
                }.get(path, set())
                values, found = bound_values(tree, diagnostic_reason_calls=diagnostics)
                direct |= direct_provider_phases(tree)
                sent, prefixes = sent | values, prefixes | found
        self.assertEqual(prefixes, {'typed_inventory_'})
        sent |= {'typed_inventory_' + video_type for video_type in VIDEO_TYPES}
        # These phases are sent by collection: if the scan misses them, it is blind and they show as "other".
        self.assertLessEqual({'initial_channel_hydration', 'typed_inventory_long', 'initial_28d_import'}, sent)
        self.assertLessEqual(NOT_SENT, sent, 'drop values the code does not use')
        self.assertFalse(direct & NOT_SENT, 'a local reason cannot become a provider quota phase')
        for phase in sorted(sent - NOT_SENT):
            self.assertEqual(quota_label('phase', phase), phase, phase)


if __name__ == '__main__':
    unittest.main()
