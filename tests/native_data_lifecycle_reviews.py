"""Small C3/C5 acceptance groups for the disposable PG17 lifecycle runner.

The runner supplies schema25_regressions.fixtures.Fixture and resets between
independent groups. Admin SQL only seeds finite synthetic source states; real
service_role review owners, lic_reader views and the GET service are exercised.
No provider callbacks, trigger disabling, native clock replacement or sleep.
"""
from datetime import datetime, timedelta
from uuid import uuid4

from db.base.postgres import Operation
from db.inventory.review_effect_batches import refresh_review_cases_batch, withdraw_review_cases_batch
from db.reviews.video_review_evidence import refresh_review_case
from db.reviews.video_type_reviews import VideoTypeReviewsRepository
from db.reviews import video_type_review_decisions as decisions
from db.inventory.manual_decision_inventory_effects import after_manual_decision_inventory
from video_type_reviews import VideoTypeReviewService

CHANNEL = 'UC' + 'r' * 22
FOREIGN = 'UC' + 's' * 22
VIDEO = 'review00001'
WINDOW = timedelta(hours=696)


def _seed(f, *, foreign=False):
    f.channel(CHANNEL)
    if foreign:
        f.channel(FOREIGN)
    young, old = f.now - timedelta(hours=1), f.now - timedelta(days=28, hours=20)
    f.rows("INSERT INTO public.video_inventory(video_id,channel_id,video_type) VALUES(%s,%s,'long')", (VIDEO, CHANNEL))
    for kind, owner, checked in [('long', CHANNEL, young), ('short', FOREIGN if foreign else CHANNEL, old)]:
        prefix = 'UULF' if kind == 'long' else 'UUSH'
        f.rows("""INSERT INTO public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,
          evidence_source,first_seen_at,last_seen_at,last_present_at)
          VALUES(%s,%s,%s,%s,'typed_playlist_scan',%s,%s,%s)""",
          (VIDEO, owner, kind, prefix + owner[2:], checked, checked, checked))
    _refresh(f)
    row = _raw(f)
    f.check(row['evidence_expires_at'] == old + WINDOW, 'review_oldest_source_deadline')
    return row


def _refresh(f, *, batch=False, removed=False):
    with f.database.transaction(Operation('lifecycle_review_acceptance', statement_timeout_ms=5000,
                                         lock_timeout_ms=250, total_timeout_ms=10000)) as tx:
        if batch:
            (withdraw_review_cases_batch if removed else refresh_review_cases_batch)(tx, [VIDEO])
        else:
            refresh_review_case(tx, VIDEO, source_removed=removed)


def _raw(f):
    return f.rows("""SELECT case_id::text,revision,last_seen_at,evidence_expires_at,
      observed_canonical_type,observed_types,observed_channels,blocked_reason,
      decision_type,decision_reason,decision_revision FROM public.video_type_review_cases WHERE video_id=%s""", (VIDEO,))[0]


def _detail(f, case_id):
    return VideoTypeReviewService(VideoTypeReviewsRepository(f.database)).detail(case_id)


def evidence_deadline(f):
    initial = _seed(f)
    deadline = initial['evidence_expires_at']
    case_id = initial['case_id']
    _refresh(f, removed=None)  # Retained native NULL flag follows the normal rebuild branch.
    f.check(_raw(f)['observed_canonical_type'] == 'long', 'review_nullable_removed_flag')
    detail = _detail(f, case_id)['case']
    f.check(detail['observed_types'] == ['long', 'short'], 'review_get_current_evidence')
    f.check(datetime.fromisoformat(detail['source_expires_at']) == deadline, 'review_get_inherited_deadline')
    # A new observation of only one contributor cannot renew the older Short.
    f.rows("""UPDATE public.video_inventory_memberships SET last_present_at=%s,last_seen_at=%s
      WHERE video_id=%s AND video_type='long'""", (f.now, f.now, VIDEO))
    _refresh(f, batch=True)
    rebuilt = _raw(f)
    f.check(rebuilt['evidence_expires_at'] == deadline, 'review_batch_no_false_freshness')
    f.check(rebuilt['last_seen_at'] >= initial['last_seen_at'], 'review_local_rebuild_clock')
    f.check(rebuilt['revision'] == initial['revision'], 'review_same_evidence_revision')
    # The retained copy has its own deadline if its contributors later vanish.
    f.rows("DELETE FROM public.video_inventory_memberships WHERE video_id=%s AND video_type='short'", (VIDEO,))
    f.check(_raw(f)['evidence_expires_at'] == deadline, 'review_deadline_survives_source_loss')
    # Deterministic elapsed-state fixture: exercise protected visibility without a timer.
    expired = f.now - timedelta(seconds=1)
    f.rows('UPDATE public.video_type_review_cases SET evidence_expires_at=%s WHERE case_id=%s::uuid', (expired, case_id))
    view = f.database.read_row('review_lifecycle_view', """SELECT observed_canonical_type,observed_types,
      observed_channels,blocked_reason,source_expires_at FROM readable.video_type_review_cases WHERE case_id=%s::uuid""", (case_id,))
    f.check(view == dict(observed_canonical_type=None, observed_types=[], observed_channels=[],
                        blocked_reason=None, source_expires_at=None), 'review_expired_view_masks_copy')
    f.check(_detail(f, case_id)['case']['observed_types'] == [], 'review_get_masks_expired_copy')
    f.check(_raw(f)['observed_types'] == ['long', 'short'], 'review_read_does_not_mutate_retained_copy')
    _refresh(f, removed=True)
    withdrawn = _raw(f)
    f.check(withdrawn['evidence_expires_at'] is None and withdrawn['observed_types'] == []
            and withdrawn['observed_channels'] == [] and withdrawn['observed_canonical_type'] is None,
            'review_scalar_withdraws_pair')
    f.observe()
    return dict(case='review_evidence_deadline', checks=f.checks)


def expired_blocker(f):
    initial = _seed(f, foreign=True)
    case_id = initial['case_id']
    f.check(_detail(f, case_id)['case']['blocked_reason'] == 'channel_conflict', 'review_current_reason')
    old = f.now - timedelta(days=30)
    f.rows("""UPDATE public.video_inventory_memberships SET first_seen_at=%s,last_present_at=%s,last_seen_at=%s
      WHERE video_id=%s""", (old, old, old, VIDEO))
    f.rows('UPDATE public.video_type_review_cases SET evidence_expires_at=%s WHERE case_id=%s::uuid', (old + WINDOW, case_id))
    detail = _detail(f, case_id)['case']
    f.check(detail['video_id'] == VIDEO and detail['observed_types'] == [], 'review_retains_attached_identity')
    f.check(detail['can_decide'] is False and detail['blocked_reason'] == 'source_unavailable',
            'review_hidden_reason_cannot_enable_decision')
    before = f.snapshot()
    result = VideoTypeReviewsRepository(f.database).decide(case_id, str(uuid4()), initial['revision'], 'short', 'Synthetic decision')
    f.check(result['outcome'] == 'blocked', 'review_writer_keeps_raw_conflict_guard')
    f.check(f.snapshot() == before, 'review_denied_decision_unchanged')
    f.observe()
    return dict(case='review_expired_blocker', checks=f.checks)


def local_decision(f):
    initial = _seed(f)
    case_id = initial['case_id']
    expired = f.now - timedelta(seconds=1)
    f.rows('UPDATE public.video_type_review_cases SET evidence_expires_at=%s WHERE case_id=%s::uuid', (expired, case_id))
    service = VideoTypeReviewService(VideoTypeReviewsRepository(f.database))
    f.check(service.detail(case_id)['case']['can_decide'] is True, 'review_local_decision_guard_allows')
    command = dict(request_id=str(uuid4()), expected_revision=initial['revision'], video_type='short', reason='Synthetic owner choice')
    result = service.decide(case_id, command)
    f.check(result['mutation']['replayed'] is False and result['case']['decision']['reason'] == command['reason'],
            'review_local_decision_saved')
    after = _raw(f)
    f.check(after['evidence_expires_at'] == expired and after['observed_canonical_type'] is None,
            'review_decision_never_renews_evidence')
    replay = service.decide(case_id, command)
    f.check(replay['mutation']['replayed'] is True and _raw(f) == after, 'review_decision_replay_no_renewal')
    _refresh(f, batch=True, removed=True)
    withdrawn = _raw(f)
    f.check(withdrawn['evidence_expires_at'] is None and withdrawn['observed_types'] == []
            and withdrawn['decision_type'] == 'short' and withdrawn['decision_reason'] == command['reason'],
            'review_batch_withdraw_keeps_manual_work')
    detail = service.detail(case_id)
    f.check(detail['case']['canonical_type'] == 'short' and detail['history'][0]['reason'] == command['reason'],
            'review_get_keeps_manual_history')
    f.observe()
    return dict(case='review_local_decision', checks=f.checks)


def decision_clock_boundary(f):
    initial = _seed(f)
    # Reproduce an in-flight decision with captured time before the deadline and
    # its SQL tail after it. Only fixture data ages; native clocks never change.
    decided_at = f.now - timedelta(seconds=2)
    deadline = f.now - timedelta(seconds=1)
    case_id = initial['case_id']
    f.rows('UPDATE public.video_type_review_cases SET evidence_expires_at=%s WHERE case_id=%s::uuid',
           (deadline, case_id))
    args = dict(case_id=case_id, video_id=VIDEO, channel_id=CHANNEL, video_type='short',
                reason='Synthetic boundary choice', decided_at=decided_at,
                revision=initial['revision'] + 1, expected_revision=initial['revision'], request_id=str(uuid4()))
    with f.database.transaction(Operation('lifecycle_review_boundary', statement_timeout_ms=5000,
                                         lock_timeout_ms=250, total_timeout_ms=10000)) as tx:
        f.check(tx.fetch_one(decisions._ADVISORY, (VIDEO,))['acquired'] is True,
                'review_boundary_owner_lock')
        old = tx.fetch_one(decisions._INVENTORY, (VIDEO,))
        tx.fetch_one(decisions._CASE_LOCK, (case_id,))
        tx.execute(decisions._DECISION, args)
        tx.execute(decisions._HISTORY, args)
        changed = tx.returning_one(decisions._CANONICAL_EFFECT, args)
        observed = tx.fetch_one('SELECT observed_canonical_type,evidence_expires_at FROM public.video_type_review_cases WHERE case_id=%s::uuid',
                                (case_id,))
        f.check(observed['observed_canonical_type'] == 'short' and observed['evidence_expires_at'] == deadline,
                'review_boundary_uses_captured_decision_time')
        after_manual_decision_inventory(tx, old_text=old['effect_old'], new_text=changed['value'])
    saved = _raw(f)
    f.check(saved['decision_type'] == 'short' and saved['evidence_expires_at'] == deadline,
            'review_boundary_helper_accepts_same_decision_time')
    sources = f.database.read_row('review_boundary_source_projection', '''SELECT c.observed_types,
      c.source_expires_at AS review_expires_at,i.source_expires_at AS inventory_expires_at
      FROM readable.video_type_review_cases c LEFT JOIN readable.video_inventory i ON i.video_id=c.video_id
      WHERE c.case_id=%s::uuid''', (case_id,))
    f.check(sources['observed_types'] == [] and sources['review_expires_at'] is None,
            'review_boundary_view_hides_expired_evidence')
    # Detail carries the minimum admitted review/inventory deadline. The Short
    # membership seeded above is still fresh even after the review copy expires.
    inventory_deadline = datetime.fromisoformat(sources['inventory_expires_at'])
    f.check(inventory_deadline == f.now + timedelta(hours=4),
            'review_boundary_inventory_keeps_own_deadline')
    detail = _detail(f, case_id)['case']
    f.check(detail['decision']['video_type'] == 'short' and detail['observed_types'] == [],
            'review_boundary_get_keeps_local_choice_hides_evidence')
    f.check(datetime.fromisoformat(detail['source_expires_at']) == inventory_deadline,
            'review_boundary_get_uses_remaining_inventory_deadline')
    f.observe()
    return dict(case='review_decision_clock_boundary', checks=f.checks)


CASES = (evidence_deadline, expired_blocker, local_decision, decision_clock_boundary)


def run(f):
    results = []
    for index, case in enumerate(CASES):
        if index:
            f.reset()
        results.append(case(f))
    return results
