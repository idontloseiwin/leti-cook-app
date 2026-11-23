"""Shared helpers for the source-layer tests: synthetic fixtures, a fake sender, canonical results.

No network: responses come from tests/fixtures/provider_sources/youtube/<method>/<case>.json
(synthetic, documented shapes, header source_url/synthetic/case). Owner-OAuth families
 live under tests/fixtures/provider_sources/youtube_owner/: their request has
"context": "owner" and runs with a fictitious owner binding and token (no sign-in exists);
a media answer gives its body as "body_text".
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
FIXTURES = ROOT / 'tests/fixtures/provider_sources/youtube'
OWNER_FIXTURES = ROOT / 'tests/fixtures/provider_sources/youtube_owner'
NOW = datetime(2026, 10, 29, 10, 0, 0, tzinfo=timezone.utc)

from provider_sources.acquire import acquire  # noqa: E402
from provider_sources.continuation import ContinuationCodec  # noqa: E402
from provider_credentials import OwnerBearer  # noqa: E402
from provider_sources.credentials import Credential, OwnerBinding, OwnerContext  # noqa: E402
from provider_sources.plan import plan_request  # noqa: E402
from provider_sources.registry import load_registry  # noqa: E402
from provider_sources.results import to_wire  # noqa: E402
from provider_sources.transport import Exchange  # noqa: E402
from provider_sources.wire import acquisition_request  # noqa: E402


SCOPE = 'https://www.googleapis.com/auth/'
OWNER_CHANNEL = 'UC' + 'o' * 22
OWNER_CONTEXT = OwnerContext(OwnerBinding('synthetic-binding', OWNER_CHANNEL, frozenset(
    SCOPE + name for name in ('youtube.readonly', 'youtube.force-ssl', 'yt-analytics.readonly',
                              'yt-analytics-monetary.readonly', 'youtube.channel-memberships.creator')),
    'synthetic-project'))
OWNER_TOKEN = 'synthetic-owner-token-' + 'q' * 24


def cases(method=None, root=FIXTURES):
    for path in sorted(root.glob('*/*.json')):
        if path.name == 'golden.json' or (method and path.parent.name != method):
            continue
        yield path.parent.name, path.stem, json.loads(path.read_text(encoding='utf-8'))


def load(method, name, root=FIXTURES):
    return json.loads((root / method / f'{name}.json').read_text(encoding='utf-8'))


def body_bytes(response):
    if 'body_text' in response:
        return response['body_text'].encode('utf-8')
    return json.dumps(response['body'], ensure_ascii=False).encode('utf-8')


class FakeSender:
    """Answers planned requests from fixture responses in order; records the query parameters."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, plan, params, **kwargs):
        self.calls.append(dict(params))
        self.targets = getattr(self, 'targets', []) + [(kwargs.get('endpoint') or plan.endpoint,
                                                        kwargs.get('label') or plan.ledger_endpoint_label)]
        response = self.responses[len(self.calls) - 1]
        return Exchange(status=response['status'], content=body_bytes(response),
                        date_header=response.get('headers', {}).get('Date'),
                        retry_after=response.get('headers', {}).get('Retry-After'),
                        attempt_id=f'00000000-0000-4000-8000-{len(self.calls):012d}')


def plan_for(document, registry=None):
    owner = OWNER_CONTEXT if document.get('context') == 'owner' else None
    return plan_request(acquisition_request(document, owner_context=owner), registry or load_registry())


def credential_for(plan):
    if plan.owner:
        return Credential(plan.context, OwnerBearer(OWNER_TOKEN))
    return Credential('public_key', None)


def run_case(fixture, *, codec=None, continuation=None, cap=None):
    registry = load_registry()
    plan = plan_for(fixture['request'], registry)
    sender = FakeSender(fixture['responses'])
    result = acquire(plan, registry=registry, credential=credential_for(plan),
                     cancelled=threading.Event(), deadline=time.monotonic() + 60, attempt_recorder=None,
                     cap=cap, codec=codec or ContinuationCodec(), continuation=continuation, send=sender,
                     clock=lambda: NOW)
    return plan, result, sender


def canonical(result):
    """The wire result with values bound to the registry revision masked (a bump never rewrites goldens).

    test_provider_parse_golden checks the masked values against the plan and the registry separately.
    """
    value = to_wire(result)
    if value['continuation'] is not None:
        value['continuation'] = '<encrypted continuation>'
    value['plan_fingerprint'] = '<plan fingerprint>'
    for page in value['pages']:
        page['plan_fingerprint'] = '<plan fingerprint>'
        page['registry_revision'] = '<registry revision>'
    return value
