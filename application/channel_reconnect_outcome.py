"""Pure reconnect response values shared by provider reads and collection."""
from dataclasses import dataclass, field
from saved_work import channel_id
from youtube_errors import ProviderOutcome

_BLOCK = frozenset(('channel_not_found', 'needs_input', 'unsupported_legacy_channel_link',
                    'provider_key_rejected', 'provider_access_denied'))
_TRANSIENT = frozenset(('provider_unavailable', 'channel_lookup_unavailable',
                        'initial_inventory_count_unavailable', 'provider_rate_limited'))


@dataclass(frozen=True)
class ReconnectResponse:
    # Do not expose source data/locator in repr or an exception message.
    channel: dict | None = field(default=None, repr=False)
    code: str | None = None
    retry_seconds: int | None = None
    dispatched: bool = True

    def __post_init__(self):
        if type(self.dispatched) is not bool:
            raise ValueError('reconnect_response_invalid')
        if not self.dispatched:
            valid = self.channel is None and self.code is None and self.retry_seconds is None
        elif self.channel is not None:
            valid = type(self.channel) is dict and self.code is None and self.retry_seconds is None
            if valid:
                channel_id(self.channel.get('channel_id'))
        else:
            valid = self.code in _BLOCK | _TRANSIENT | {'provider_quota_exceeded'}
            if self.retry_seconds is not None:
                valid = (valid and self.code in ('provider_rate_limited', 'provider_unavailable')
                         and type(self.retry_seconds) is int and 1 <= self.retry_seconds <= 60)
        if not valid:
            raise ValueError('reconnect_response_invalid')


def reconnect_refusal(outcome: ProviderOutcome) -> ReconnectResponse:
    """Fixed provider evidence only; generic 404 never proves a missing channel."""
    if not isinstance(outcome, ProviderOutcome) or outcome.quota_bucket != 'general':
        raise ValueError('reconnect_provider_outcome_invalid')
    if outcome.kind == 'daily_quota':
        return ReconnectResponse(code='provider_quota_exceeded')
    if outcome.kind == 'key_refused' and outcome.key_code in ('provider_key_rejected', 'provider_access_denied'):
        return ReconnectResponse(code=outcome.key_code)
    if outcome.kind in ('rate_limit', 'transient'):
        delay = outcome.retry_after_seconds
        if delay is not None and (type(delay) is not int or not 0 <= delay <= 60):
            raise ValueError('reconnect_provider_delay_invalid')
        return ReconnectResponse(code=('provider_rate_limited' if outcome.kind == 'rate_limit'
                                       else 'provider_unavailable'),
                                 retry_seconds=max(1, delay) if delay is not None else 60)
    if outcome.kind == 'resource_missing':
        return ReconnectResponse(code=('channel_not_found' if outcome.resource == 'channel'
                                       else 'channel_lookup_unavailable'))
    raise ValueError('reconnect_provider_outcome_invalid')

