"""Passive attribution for existing playlist receipts. No budget, storage or network calls."""
from contextlib import contextmanager
from contextvars import ContextVar

_http = ContextVar('playlist_http_cost', default=None)
_receipt = ContextVar('playlist_receipt_observation', default=None)


@contextmanager
def wire_cost():
    costs = {'attempts': 0, 'general': 0, 'retries': 0}
    token = _http.set(costs)
    try:
        yield costs
    finally:
        _http.reset(token)


def http_start(bucket, units, retry):
    costs = _http.get()
    if costs is not None and bucket == 'general' and type(units) is int:
        costs['attempts'] += 1
        costs['general'] += units
        costs['retries'] += int(retry is not None)


def purpose(claim, plan, *, head=False, serve_signal=True, initial=False, operator=False):
    signal = (serve_signal and plan.get('head_signal')) if head else plan.get('flags', {}).get('event')
    if initial:
        reason = 'initial'
    elif operator:
        reason = 'operator'
    elif getattr(claim, 'requested_out_of_schedule', False) is True:
        reason = 'targeted_before_activation' if plan.get('enabled') is False else 'targeted'
    elif plan.get('enabled') is False:
        reason = 'before_activation'
    elif plan.get('enabled') is not True:
        reason = 'unknown'
    elif not head and plan.get('manual'):
        reason = 'manual'
    elif signal:
        reason = 'signal'
    elif head or claim.scan_kind == 'discovery':
        reason = 'periodic_head'
    elif claim.scan_kind == 'initial_inventory':
        reason = 'initial_candidate'
    else:
        reason = 'periodic_full'
    return dict(reason=reason, signal=bool(signal), prior_head=plan.get('prior_head'),
                scheduled_head=bool(plan.get('head_scheduled')), head=head,
                format=claim.video_type, channel=getattr(claim, 'channel_id', None), epoch=claim.lease_channel_epoch, period_seconds=plan.get('period_seconds'))


@contextmanager
def bound(value):
    token = _receipt.set(value)
    try:
        yield value
    finally:
        _receipt.reset(token)


def settlement(prepared, response):
    facts = getattr(prepared, 'observation', None)
    if not isinstance(facts, dict):
        return bound(None)
    return bound(dict(facts, transition=prepared.identity['transition_id'],
                      scan=prepared.scan_id, first=prepared.expected_page_token is None,
                      cost=getattr(response, 'wire_cost', None)))


def replayed(result):
    reply = dict(result, replayed=True)
    reply.pop('list_observation', None)
    return reply


def annotated(result, transition, **facts):
    active = _receipt.get()
    if not active or active['transition'] != str(transition):
        return result
    return dict(result, list_observation=dict(active, **facts))
