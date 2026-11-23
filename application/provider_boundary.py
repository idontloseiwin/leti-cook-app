"""The boundary between stored-data queries and explicit provider acquisition.

A stored-data query (the research backend under Library and the workshop) runs
inside ``provider_forbidden()``. The shared provider gate and the YouTube
transport refuse every request in that context before a quota attempt is
recorded or a byte is sent, so reading stored data can never start a provider
request (02 source-layer design, section 5.1).

This module imports nothing from the application. A read owner may use it
without being able to reach the transport.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import functools

PROVIDER_FORBIDDEN = "provider_forbidden"
_FORBIDDEN = ContextVar("lic_provider_forbidden", default=False)


@contextmanager
def provider_forbidden():
    """Run the enclosed work as stored-data-only; nested scopes stay forbidden."""
    token = _FORBIDDEN.set(True)
    try:
        yield
    finally:
        _FORBIDDEN.reset(token)


def provider_forbidden_active():
    return _FORBIDDEN.get() is True


def stored_data_only(function):
    """Decorate a stored-data entry point; worker threads inherit the context."""
    @functools.wraps(function)
    def guarded(*args, **kwargs):
        with provider_forbidden():
            return function(*args, **kwargs)
    return guarded
