"""Research-only credentials; never grant the owner/session authorization state."""
import hashlib
import re
import secrets
import time

from db.agent_access import AgentAccessRepository
from db.self_hosted_auth import AuthStoreError

TOKEN = re.compile(r'lic_ro_[A-Za-z0-9_-]{43}\Z')
READ_ROUTES = frozenset({
    ('GET', '/research/capabilities'), ('GET', '/research/sources'),
    ('GET', '/research/contracts'),
    ('POST', '/research/queries/validate'), ('POST', '/research/queries/execute'),
    ('POST', '/research/operations/execute'),
    ('POST', '/research/graphs/validate'), ('POST', '/research/graphs/execute'),
})
# The MCP endpoint forwards each tool call to READ_ROUTES with the same key.
# GET and DELETE only answer 405: the server keeps no stream or session.
MCP_ROUTES = frozenset({('POST', '/mcp'), ('GET', '/mcp'), ('DELETE', '/mcp')})
AGENT_ROUTES = READ_ROUTES | MCP_ROUTES


def digest(token):
    return hashlib.sha256(b'lic-research-agent-v1\0' + token.encode('ascii')).hexdigest()


def issue(owner, name, days=30, *, clock=time.time):
    if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 80
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or type(days) is not int or not 1 <= days <= 365):
        raise AuthStoreError('invalid_agent_key_options')
    binding = owner.agent_binding()
    if not binding:
        raise AuthStoreError('owner_access_not_configured')
    store = AgentAccessRepository(owner.path)
    store.initialize()
    token = 'lic_ro_' + secrets.token_urlsafe(32)
    now = int(clock())
    metadata = store.create(name.strip(), digest(token), binding, now, now + days * 86400)
    return metadata, token


def check(owner, token, *, clock=time.time, resource=None):
    if isinstance(token, str) and token.startswith('lic_at_'):
        # An access token from an owner-approved connection (agent_oauth.py),
        # valid only for the resource it was issued for; without one, refused.
        from agent_oauth import check_access
        return check_access(owner, token, resource=resource, clock=clock)
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        return None
    binding = owner.agent_binding()
    if not binding:
        return None
    row = AgentAccessRepository(owner.path).lookup(digest(token), binding, int(clock()))
    return 'agent.' + row['id'] if row else None
