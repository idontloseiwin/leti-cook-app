"""A supervised discovery portion; no ordinary automatic queue pass."""
from contextlib import ExitStack
import math
import os
import time

DEADLINE_ENV = 'LIC_DISCOVERY_DEADLINE_MONOTONIC'
COLLECTION_DEADLINE_ENV = 'LIC_COLLECTION_DEADLINE_MONOTONIC'


def _deadline(value, clock):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError('discovery_deadline_invalid') from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError('discovery_deadline_invalid')
    return value if value > clock() else None


def collection_deadline(environ, *, clock=time.monotonic):
    """Unsupervised/old callers cannot mint a discovery lifetime allowance."""
    if (environ.get('LIC_COLLECTION_SUPERVISED') != '1'
            or environ.get('LIC_DISCOVERY_ALLOWED') != '1'
            or COLLECTION_DEADLINE_ENV not in environ):
        return None
    return _deadline(environ[COLLECTION_DEADLINE_ENV], clock)


def portion_deadline(environ, *, clock=time.monotonic):
    """The parent's fixed monotonic boundary includes imports and preflight."""
    if environ.get('LIC_COLLECTION_SUPERVISED') != '1' or environ.get('LIC_DISCOVERY_ALLOWED') != '1':
        return None
    return _deadline(environ.get(DEADLINE_ENV), clock)


def run(*, environ=None, clock=time.monotonic, operator_fallback=False):
    """Borrow the already admitted provider and shared process lock."""
    environ = os.environ if environ is None else environ
    deadline = portion_deadline(environ, clock=clock)
    if deadline is None:
        return 0
    from runtime.process_collection import process_collection_configuration, process_collection_scope
    from runtime.discovery import run_discovery_portion
    from core import process_database_resources
    settings, policy = process_collection_configuration()
    with ExitStack() as scope:
        scope.enter_context(process_database_resources())
        context = scope.enter_context(process_collection_scope(settings=settings, policy=policy))
        if operator_fallback:
            from db.discovery import DiscoveryRepository
            if not DiscoveryRepository(context.database).enabled():
                return None
        summary = run_discovery_portion(context, deadline=deadline, clock=clock)
        return 1 if int(summary.get('errors') or 0) else 0
