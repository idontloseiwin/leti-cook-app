"""Process-local HTTP provenance, never a role name or request parameter.

The transport chooses the only two permitted roles. This context carries no
database connection and cannot grant additional privileges to a transaction.
"""
from contextlib import contextmanager
from contextvars import ContextVar

_HTTP_READ = ContextVar('lic_http_read', default=False)
_HTTP_METHOD = ContextVar('lic_http_method', default=None)


def is_http_read():
    return _HTTP_READ.get()


def http_request_method():
    return _HTTP_METHOD.get()


@contextmanager
def http_method_scope(method):
    token = _HTTP_METHOD.set(method.upper())
    try:
        yield
    finally:
        _HTTP_METHOD.reset(token)


@contextmanager
def http_read_scope():
    token = _HTTP_READ.set(True)
    try:
        yield
    finally:
        _HTTP_READ.reset(token)


@contextmanager
def collection_scope():
    """A background collector owns its work independently of its HTTP trigger."""
    token = _HTTP_READ.set(False)
    method_token = _HTTP_METHOD.set(None)
    try:
        yield
    finally:
        _HTTP_METHOD.reset(method_token)
        _HTTP_READ.reset(token)
