"""Count hints cannot replace full coverage: native claims with a 7-day policy.

Only the disposable fixture's old scan timestamps/due dates are moved. Source
pages, metadata writes, count decisions and full membership absence use their
production transaction owners. This is not a supervisor/timing benchmark.
"""
from datetime import timedelta
import os
from uuid import uuid4

from collection_schedule import discovery_schedules
from db.base.postgres import PostgresDatabase
from db.channel.channel_sync import ChannelSyncRepository
from db.discovery import DiscoveryRepository
from db.discovery_counts import settle_count_heads
from db.inventory.typed_inventory import TypedInventoryRepository


CHANNEL = 'UC' + 'fxcoverage'.ljust(22, '0')
OLD = [f'fxcvold{i:04}' for i in range(10)]
HEAD, DEEP, NET_ZERO = 'fxcvhead001', 'fxcvdeep001', 'fxcvzero001'


def run_checks(admin, database):
    if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE') != '1':
        raise RuntimeError('disposable_cluster_required')
    # The actual SQL claimant receives the accepted seven-day process snapshot;
    # changing Python globals or the production/default template is unnecessary.
    with PostgresDatabase(database.config, admission=lambda _: None,
                          cadence_snapshot=discovery_schedules().cadence_snapshot()) as selected:
        return _run(admin, selected)


def _run(admin, database):
    checks = []

    def scalar(statement, parameters=()):
        row = admin.execute(statement, parameters).fetchone()
        return next(iter(row.values())) if isinstance(row, dict) else row[0]

    def check(condition, name):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    def now():
        return scalar('SELECT clock_timestamp()')

    channel = ChannelSyncRepository(database)
    channel.insert_channel(dict(channel_id=CHANNEL, title='Discovery coverage synthetic',
        user_input='https://www.youtube.com/channel/' + CHANNEL,
        initial_long_inventory_status='completed', initial_28d_stats_status='completed',
        video_count=10, channel_metadata_status='ok',
        channel_metadata_checked_at=(now() - timedelta(minutes=1)).isoformat()), permission='accepted')
    epoch = scalar('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s', (CHANNEL,))
    typed = TypedInventoryRepository(database)
    typed.prepare_runtime(limit=1000, enable=True)
    # Synthetic fixture bypass only: real preparation/cutover is tested separately.
    admin.execute('UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,now()) WHERE singleton')
    check(database.cadence_snapshot.inventory_by_type.long.reconciliation_seconds == 604800
          and database.cadence_snapshot.inventory_by_type.short.reconciliation_seconds == 604800,
          'claimant_has_seven_day_per_format_snapshot')

    def claim(kind):
        admin.execute("UPDATE public.channel_playlist_state SET next_due_at=now()-interval '1 second' WHERE channel_id=%s AND video_type=%s",
                      (CHANNEL, kind))
        rows = typed.claim_channels([CHANNEL], video_types=[kind], only_due=True,
            worker_id='Discovery coverage', run_id=str(uuid4()),
            expected_channel_scopes=[dict(channel_id=CHANNEL, control_epoch=epoch)])
        if len(rows) != 1:
            raise AssertionError('coverage_fixture_claimable')
        return rows[0]

    publications = {}

    def finish(claimed, identities, *, status):
        scan_id = str(uuid4())
        typed.begin_scan(claimed, scan_id=scan_id, scan_boundary_at=now())
        result = typed.settle_page(claimed, scan_id=scan_id, transition_id=str(uuid4()),
            expected_page_token=None, next_page_token=None, page_status=status,
            items=[dict(video_id=identity,published_at=publications.get(identity)) for identity in identities], source_checked_at=now())
        return scan_id, result

    def write_count(value):
        observed = now().isoformat()
        result = channel.update_metadata_batch([dict(channel_id=CHANNEL, control_epoch=epoch,
            values=dict(video_count=value, channel_metadata_checked_at=observed, channel_metadata_status='ok'))])
        check(result == {CHANNEL: 'updated'}, 'native_count_update_accepted')
        return scalar('SELECT count_reconcile_token FROM public.channels WHERE channel_id=%s', (CHANNEL,))

    long_initial = claim('long')
    short_initial = claim('short')
    check(long_initial.scan_kind in ('initial_inventory', 'full_reconciliation')
          and short_initial.scan_kind == 'initial_inventory', 'new_sources_require_actual_initial_coverage')
    long_full, _ = finish(long_initial, OLD, status='complete')
    short_full, _ = finish(short_initial, [], status='empty')
    age_six_days = now() - timedelta(days=6)
    admin.execute('UPDATE public.channel_video_inventory_scans SET started_at=%s,scan_boundary_at=%s WHERE scan_id=ANY(%s::uuid[])',
                  (age_six_days, age_six_days, [long_full, short_full]))

    # Provider truth: ten old videos -> remove OLD[0], add HEAD and DEEP.
    # The real count delta is +1, while the bounded head reveals only HEAD.
    publications[HEAD] = now().isoformat()
    token = write_count(11)
    head = claim('long')
    check(head.scan_kind == 'discovery', 'six_day_coverage_allows_head_despite_recent_completion')
    _, result = finish(head, [HEAD, OLD[1]], status='boundary')
    check(result['new_video_ids'] == [HEAD] and result['coverage'] == 'partial', 'head_finds_one_without_complete_coverage')
    short_head = claim('short')
    check(short_head.scan_kind == 'discovery', 'short_uses_its_own_complete_history')
    finish(short_head, [], status='empty')
    decision = settle_count_heads(database, CHANNEL, video_types=('long', 'short'))
    check(decision['settled'] and decision['new_count'] == 1 and not decision['full_requested'],
          'plus_two_minus_one_delta_equals_N_is_only_head_ack')
    check(scalar('SELECT count(*) FROM public.video_inventory WHERE video_id=%s', (DEEP,)) == 0,
          'matching_count_does_not_prove_deep_video_discovered')
    check(scalar('SELECT started_at FROM public.channel_video_inventory_scans WHERE scan_id=%s', (long_full,)) == age_six_days,
          'new_head_does_not_renew_full_coverage_clock')
    check(scalar("SELECT membership_status FROM public.video_inventory_memberships WHERE video_id=%s AND video_type='long'", (OLD[0],)) == 'present',
          'head_does_not_infer_removed_membership')

    # Another replacement in the depth has net zero: remove OLD[1], add NET_ZERO.
    # An unchanged channel count cannot create evidence or demand for that ID.
    same = write_count(11)
    check(same == token and not settle_count_heads(database, CHANNEL, video_types=('long', 'short'))['settled'],
          'net_zero_count_has_no_new_generation_or_false_full_claim')
    recent_head = claim('long')
    check(recent_head.scan_kind == 'discovery', 'unchanged_count_still_allows_independent_head')
    finish(recent_head, [HEAD, OLD[2]], status='boundary')
    check(scalar('SELECT count(*) FROM public.video_inventory WHERE video_id=ANY(%s)', ([DEEP, NET_ZERO],)) == 0,
          'recent_head_and_net_zero_leave_deep_ids_unproven')

    # Age only the successful full start; its completion and heads remain fresh.
    age_over_seven_days = now() - timedelta(days=7, hours=1)
    admin.execute('UPDATE public.channel_video_inventory_scans SET started_at=%s,scan_boundary_at=%s WHERE scan_id=%s',
                  (age_over_seven_days, age_over_seven_days, long_full))
    overdue = claim('long')
    check(overdue.scan_kind == 'full_reconciliation', 'seven_day_start_age_forces_full_despite_fresh_heads_and_end')
    full_id, result = finish(overdue, [*OLD[2:], HEAD, DEEP, NET_ZERO], status='complete')
    check(result['coverage'] == 'complete' and set(result['new_video_ids']) == {DEEP, NET_ZERO},
          'independent_full_discovers_both_compensated_deep_additions')
    check(scalar("SELECT count(*) FROM public.video_inventory_memberships WHERE video_id=ANY(%s) AND membership_status='absent'",
                 (OLD[:2],)) == 2, 'only_complete_full_marks_two_removed_memberships')
    unaffected_short = claim('short')
    check(unaffected_short.scan_kind == 'discovery', 'aging_long_history_does_not_age_short_history')
    finish(unaffected_short, [], status='empty')

    # A recent success from an old ownership epoch cannot cover a new epoch.
    epoch = scalar('UPDATE public.channel_collection_state SET control_epoch=nextval(\'public.channel_collection_epoch_seq\') WHERE channel_id=%s RETURNING control_epoch', (CHANNEL,))
    different_epoch = claim('long')
    check(different_epoch.scan_kind == 'full_reconciliation',
          'recent_full_from_other_epoch_cannot_admit_head')
    check(scalar('SELECT discovery_channel_epoch FROM public.channel_video_inventory_scans WHERE scan_id=%s', (full_id,)) != epoch,
          'epoch_test_preserves_original_full_evidence')
    return checks
