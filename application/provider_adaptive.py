"""Pure bounded slowdown policy; the admission file guard owns serialization."""
import math

RECOVERY_SECONDS = 30


def initial():
    return {'epoch': 0, 'probe_needed': False, 'probe': None, 'healthy_since': None}


def validate(value):
    if (type(value) is not dict
            or set(value) != {'epoch', 'probe_needed', 'probe', 'healthy_since'}
            or type(value['epoch']) is not int or not 0 <= value['epoch'] < 2**53
            or type(value['probe_needed']) is not bool
            or (value['probe'] is not None and not isinstance(value['probe'], str))
            or (not value['probe_needed'] and value['probe'] is not None)
            or (value['probe_needed'] and value['healthy_since'] is not None)):
        raise ValueError('provider_adaptive_state_invalid')
    healthy = value['healthy_since']
    if healthy is not None and (type(healthy) not in (int, float)
            or not math.isfinite(healthy) or not 0 <= healthy <= 1e15):
        raise ValueError('provider_adaptive_state_invalid')


def may_admit(state, now):
    value = state['adaptive']
    return now >= state['cooldown_until'] and (not value['probe_needed'] or value['probe'] is None)


def admitted(state, identity):
    value = state['adaptive']
    if value['probe_needed']:
        if value['probe'] is not None:
            raise ValueError('provider_adaptive_probe_already_active')
        value['probe'] = identity
    return value['epoch']


def forgotten(state, identity):
    value = state['adaptive']
    if value['probe'] == identity:
        # No HTTP outcome is inferred from process exit or permit release.
        value['probe'] = None


def observe(state, identity, issued_epoch, *, now, success, rate_seconds, jitter):
    """One already observed HTTP outcome, never a request/retry or quota budget."""
    value = state['adaptive']
    if type(success) is not bool or (rate_seconds is not None and
            (type(rate_seconds) not in (int, float) or not math.isfinite(rate_seconds)
             or not 1 <= rate_seconds <= 60 or success)):
        raise ValueError('provider_adaptive_outcome_invalid')
    if (type(jitter) not in (int, float) or not math.isfinite(jitter) or not 0 <= jitter <= 1
            or type(issued_epoch) is not int or not 0 <= issued_epoch <= value['epoch']):
        raise ValueError('provider_adaptive_outcome_invalid')
    # Old successes cannot recover a new generation. An old rate response
    # still carries a current bounded Retry-After, without another reduction.
    if issued_epoch != value['epoch']:
        if rate_seconds is None:
            return False
        state['cooldown_until'] = max(state['cooldown_until'],
                                      now + min(60, rate_seconds + jitter))
        # Preserve an active probe's identity. Its response cannot reopen the
        # gate before this extended pause; no second probe runs alongside it.
        value.update(probe_needed=True, healthy_since=None)
        return True
    if rate_seconds is not None:
        if value['epoch'] >= 2**53 - 1:
            raise ValueError('provider_adaptive_epoch_exhausted')
        state['effective'] = max(1, state['effective'] // 2)
        delay = min(60, max(rate_seconds, min(60, 2 ** min(value['epoch'], 6))) + jitter)
        state['cooldown_until'] = max(state['cooldown_until'], now + delay)
        value.update(epoch=value['epoch'] + 1, probe_needed=True, probe=None, healthy_since=None)
        return True
    if value['probe_needed']:
        if value['probe'] != identity:
            return False
        value['probe'] = None
        if success and now >= state['cooldown_until']:
            value.update(probe_needed=False, healthy_since=now)
        elif not success:
            # A failed probe does not restore C; avoid a tight stream of probes.
            state['cooldown_until'] = max(state['cooldown_until'], now + 1)
        # Success during a pause extended by a late sibling leaves probe_needed
        # set. A new probe can only be issued after that pause, under the guard.
        return True
    if not success:
        value['healthy_since'] = None
    elif value['healthy_since'] is None:
        value['healthy_since'] = now
    elif now - value['healthy_since'] >= RECOVERY_SECONDS:
        state['effective'] = min(state['concurrency'], state['effective'] + 1)
        value['healthy_since'] = now
    return True
