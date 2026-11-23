"""MCP authorization (OAuth 2.1) for agent programs: the owner clicks Allow, no key is copied.

Follows the MCP authorization specification: protected resource metadata (RFC 9728),
authorization server metadata (RFC 8414), dynamic client registration (RFC 7591) for
public clients, authorization code with mandatory S256 PKCE (RFC 7636), resource
indicators (RFC 8707) and rotating refresh tokens. Redirects go only to this computer
(loopback HTTP) or to a desktop application's own scheme. Hashes only are stored.
"""
import base64
import hashlib
import html
import json
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from db.agent_oauth import AgentOAuthRepository
from db.self_hosted_auth import AuthStoreError

SCOPE = 'research.read'
ACCESS_SECONDS = 3600
LIFETIMES = (7, 30, 90, 365)
DEFAULT_DAYS = 30
ACCESS = re.compile(r'lic_at_[A-Za-z0-9_-]{43}\Z')
REFRESH = re.compile(r'lic_rt_[A-Za-z0-9_-]{43}\Z')
CODE = re.compile(r'[A-Za-z0-9_-]{43}\Z')
VERIFIER = re.compile(r'[A-Za-z0-9._~-]{43,128}\Z')
CHALLENGE = re.compile(r'[A-Za-z0-9_-]{43}\Z')
CLIENT_ID = re.compile(r'lic-client-[0-9a-f]{32}\Z')
# Only supported desktop agent handlers; arbitrary OS/browser handlers can forward to websites.
DESKTOP_SCHEMES = frozenset({'cursor', 'vscode', 'vscode-insiders'})
LOOPBACK_NAMES = frozenset({'127.0.0.1', 'localhost', '::1'})
METADATA_PATHS = frozenset({'/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp',
                            '/.well-known/oauth-authorization-server'})
# Answered by SelfHostedAuthMiddleware before any route, without a session or key.
PUBLIC_PATHS = frozenset({'/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp',
                          '/.well-known/oauth-authorization-server', '/oauth/register', '/oauth/token'})
assert METADATA_PATHS < PUBLIC_PATHS


class OAuthError(Exception):
    def __init__(self, code, status=400, description=None):
        super().__init__(code)
        self.code, self.status, self.description = code, status, description


class RedirectError(OAuthError):
    """An authorize error for a verified redirect target: report it to the program."""

    def __init__(self, code, redirect, state):
        super().__init__(code, 302)
        self.redirect, self.state = redirect, state


def digest(kind, value):
    return hashlib.sha256(b'lic-agent-oauth-v1\0' + kind.encode() + b'\0' + value.encode('ascii')).hexdigest()


def resource_url(origin):
    return origin + '/mcp'


def protected_resource_metadata(origin):
    return {'resource': resource_url(origin), 'authorization_servers': [origin], 'scopes_supported': [SCOPE],
            'bearer_methods_supported': ['header'], 'resource_name': 'Leti Cook research (read-only)'}


def authorization_server_metadata(origin):
    return {'issuer': origin, 'authorization_endpoint': origin + '/oauth/authorize',
            'token_endpoint': origin + '/oauth/token', 'registration_endpoint': origin + '/oauth/register',
            'response_types_supported': ['code'], 'grant_types_supported': ['authorization_code', 'refresh_token'],
            'code_challenge_methods_supported': ['S256'], 'token_endpoint_auth_methods_supported': ['none'],
            'scopes_supported': [SCOPE], 'authorization_response_iss_parameter_supported': True}


def challenge_header(origin):
    return ('Bearer resource_metadata="' + origin + '/.well-known/oauth-protected-resource/mcp", scope="' + SCOPE + '"')


def _redirect(value):
    """Parsed components of an acceptable redirect URI, or None.

    One strict parse for registration and authorization alike: no userinfo,
    fragment, control or space characters, and for HTTP the authority must be
    exactly a loopback host with an optional valid port (RFC 8252 sections 7.3, 8.4).
    """
    if (type(value) is not str or not 1 <= len(value) <= 512
            or any(not 33 <= ord(c) <= 126 for c in value) or '\\' in value):
        return None
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        return None
    if url.fragment or '#' in value or '@' in url.netloc or url.username is not None or url.password is not None:
        return None
    if url.scheme == 'http':
        host = (url.hostname or '').lower()
        if host not in LOOPBACK_NAMES or (port is not None and not 1 <= port <= 65535):
            return None
        bracketed = '[' + host + ']' if ':' in host else host
        if url.netloc.lower() != (bracketed if port is None else bracketed + ':' + str(port)):
            return None  # e.g. an empty port ("localhost:") or another spelling of the authority
        return ('http', host, port, url.path, url.query)
    if (url.scheme not in DESKTOP_SCHEMES or not url.netloc or port is not None
            or not re.fullmatch(r'[A-Za-z0-9.-]+', url.netloc)):
        return None
    return (url.scheme, url.netloc, None, url.path, url.query)


def redirect_allowed(value):
    """A loopback HTTP callback on any port, or a desktop application's private scheme."""
    return _redirect(value) is not None


def redirect_matches(registered, requested):
    """The requested URI equals a registered one; a loopback port alone may differ."""
    asked = _redirect(requested)
    if asked is None:
        return False
    for value in registered:
        known = _redirect(value)
        if known is None:
            continue
        if known == asked or (asked[0] == 'http' and known[0] == 'http'
                              and known[1] == asked[1] and known[3:] == asked[3:]):
            return True
    return False


def redirect_host(value):
    url = urlsplit(value)
    return url.scheme + '://' + url.netloc if url.scheme == 'http' else url.scheme + ':'


def register(store, body, now):
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise OAuthError('invalid_client_metadata') from None
    if type(data) is not dict:
        raise OAuthError('invalid_client_metadata')
    uris = data.get('redirect_uris')
    if type(uris) is not list or not 1 <= len(uris) <= 10:
        raise OAuthError('invalid_redirect_uri')
    # RFC 7591 lets the server drop values it does not accept, such as web redirects.
    allowed = [uri for uri in uris if redirect_allowed(uri)]
    if not allowed:
        raise OAuthError('invalid_redirect_uri', description='Only this computer or a desktop application may receive the code.')
    for field, accepted in (('grant_types', {'authorization_code', 'refresh_token'}), ('response_types', {'code'})):
        value = data.get(field)
        if value is not None and (type(value) is not list or not 1 <= len(value) <= 4
                                  or any(type(item) is not str for item in value) or not set(value) <= accepted):
            raise OAuthError('invalid_client_metadata')
    for field in ('client_name', 'token_endpoint_auth_method', 'scope'):
        if data.get(field) is not None and type(data[field]) is not str:
            raise OAuthError('invalid_client_metadata')
    name = data.get('client_name')
    name = name.strip() if type(name) is str else ''
    name = ''.join(c for c in name if c.isprintable())[:80] or 'Unnamed agent program'
    identity = store.register(name, allowed, now)
    return {'client_id': identity, 'client_id_issued_at': now, 'client_name': name, 'redirect_uris': allowed,
            'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
            'token_endpoint_auth_method': 'none', 'scope': SCOPE}


def authorization_request(store, params, origin, now):
    """Validate an authorize request; return (pending request id, client) or raise.

    Errors raised before the redirect URI is trusted are shown on the page,
    never sent to an unverified redirect target.
    """
    one = lambda name: params.get(name, [None])[-1] if len(params.get(name, [])) <= 1 else '\0'
    client_id, redirect = one('client_id'), one('redirect_uri')
    if type(client_id) is not str or not CLIENT_ID.fullmatch(client_id):
        raise OAuthError('unknown_client')
    client = store.client(client_id)
    if client is None:
        raise OAuthError('unknown_client')
    if redirect is None and len(client['redirect_uris']) == 1:
        redirect = client['redirect_uris'][0]
    if type(redirect) is not str or not redirect_matches(client['redirect_uris'], redirect):
        raise OAuthError('redirect_uri_not_registered')
    state = one('state')
    if state is not None and (type(state) is not str or len(state) > 512 or '\0' in state):
        raise OAuthError('invalid_request')
    problem = None
    if one('response_type') != 'code':
        problem = 'unsupported_response_type'
    elif one('code_challenge_method') != 'S256' or not CHALLENGE.fullmatch(one('code_challenge') or ''):
        problem = 'invalid_request'
    elif one('resource') not in (None, resource_url(origin)):
        problem = 'invalid_target'
    elif one('scope') is not None and set((one('scope') or '').split()) - {SCOPE}:
        problem = 'invalid_scope'
    if problem:
        raise RedirectError(problem, redirect, state)
    request = store.open_request(client_id, redirect, one('code_challenge'), state, resource_url(origin), now)
    return request, client, redirect


def error_redirect(redirect, state, code, origin):
    query = {'error': code, 'iss': origin}
    if state:
        query['state'] = state
    return redirect + ('&' if '?' in redirect else '?') + urlencode(query)


def decide(store, request_id, allow, days, binding, origin, now):
    if not re.fullmatch(r'[0-9a-f]{32}', request_id or '') or days not in LIFETIMES or not binding:
        raise OAuthError('invalid_request')
    code = secrets.token_urlsafe(32)
    pending = store.request(request_id, now)
    if pending is None:
        raise OAuthError('agent_request_expired')
    request = store.decide(request_id, allow, digest('code', code), binding,
                           redirect_host(pending['redirect_uri']), days, now)
    if not allow:
        return error_redirect(request['redirect_uri'], request['state'], 'access_denied', origin)
    query = {'code': code, 'iss': origin}
    if request['state']:
        query['state'] = request['state']
    target = request['redirect_uri']
    return target + ('&' if '?' in target else '?') + urlencode(query)


def _pair():
    return 'lic_at_' + secrets.token_urlsafe(32), 'lic_rt_' + secrets.token_urlsafe(32)


def _answer(access, refresh, expires, now):
    return {'access_token': access, 'token_type': 'Bearer', 'expires_in': max(1, min(ACCESS_SECONDS, expires - now)),
            'refresh_token': refresh, 'scope': SCOPE}


def token(store, body, binding, origin, now):
    try:
        params = parse_qs(body.decode('ascii'), keep_blank_values=True, strict_parsing=False, max_num_fields=20)
    except (UnicodeError, ValueError):
        raise OAuthError('invalid_request') from None
    if any(len(values) != 1 for values in params.values()):
        raise OAuthError('invalid_request')
    one = lambda name: params.get(name, [None])[0]
    client_id, grant = one('client_id'), one('grant_type')
    if type(client_id) is not str or not CLIENT_ID.fullmatch(client_id) or store.client(client_id) is None:
        raise OAuthError('invalid_client', status=401)
    if one('resource') not in (None, resource_url(origin)):
        raise OAuthError('invalid_target')
    if not binding:
        raise OAuthError('invalid_grant')
    if grant == 'authorization_code':
        code, verifier, redirect = one('code'), one('code_verifier'), one('redirect_uri')
        if not CODE.fullmatch(code or '') or not VERIFIER.fullmatch(verifier or ''):
            raise OAuthError('invalid_grant')
        access, refresh = _pair()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')
        made = store.exchange_code(digest('code', code), client_id, binding=binding, resource=resource_url(origin),
                                   redirect_uri=redirect, challenge=challenge, now=now,
                                   access_digest=digest('access', access), access_expires=now + ACCESS_SECONDS,
                                   refresh_digest=digest('refresh', refresh))
        if made is None:
            raise OAuthError('invalid_grant')
        return _answer(access, refresh, made['expires_at'], now)
    if grant == 'refresh_token':
        old = one('refresh_token')
        if not REFRESH.fullmatch(old or ''):
            raise OAuthError('invalid_grant')
        access, refresh = _pair()
        rotated = store.rotate(digest('refresh', old), client_id, binding, now, digest('access', access),
                               now + ACCESS_SECONDS, digest('refresh', refresh), resource_url(origin))
        if rotated is None:
            raise OAuthError('invalid_grant')
        return _answer(access, refresh, rotated['expires_at'], now)
    raise OAuthError('unsupported_grant_type')


def check_access(owner, token_value, *, resource, clock=time.time):
    """The connection behind a valid access token for this resource (RFC 8707), or None."""
    if not isinstance(token_value, str) or not ACCESS.fullmatch(token_value) or type(resource) is not str:
        return None
    binding = owner.agent_binding()
    if not binding:
        return None
    store = AgentOAuthRepository(owner.path)
    now = int(clock())
    connection = store.lookup_access(digest('access', token_value), binding, now, resource)
    if connection is None:
        return None
    # "Last used" is informational: at most one write a minute per connection.
    if _TOUCHED.get(connection, 0) <= now - 60:
        _TOUCHED[connection] = now
        try:
            store.touch(connection, now)
        except AuthStoreError:
            pass
    return 'agent.oauth.' + connection


_TOUCHED = {}


# --- The consent page: plain HTML and one form, no script.

def consent_page(request_id, client, redirect, origin):
    target = redirect_host(redirect)
    local = urlsplit(redirect).scheme == 'http'
    where = ('a program on this computer at ' + target) if local else ('the desktop application that handles ' + target)
    options = ''.join('<option value="%d"%s>%d days</option>' % (days, ' selected' if days == DEFAULT_DAYS else '', days)
                      for days in LIFETIMES)
    escaped = html.escape
    # The application's no-referrer policy would make the form send "Origin: null",
    # which the same-origin check refuses. same-origin sends this origin and nothing
    # to other sites; the redirect target receives no referrer.
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="same-origin">
<title>Leti Cook · Allow an agent</title><link rel="stylesheet" href="/auth/assets/auth.css"></head>
<body><main><a class="brand" href="/app-explorer-v3" aria-label="Leti Cook">L</a><p class="eyebrow">AGENT ACCESS</p>
<h1>Allow “{escaped(client['name'])}”?</h1>
<p>An agent program asks to <strong>read</strong> this library through MCP: find sources, query, calculate and run
canvases. It cannot collect from YouTube, change settings, or save, edit or delete anything.</p>
<p>The answer goes to {escaped(where)}. The program chose its own name. Allow only if you started this
connection yourself just now.</p>
<p>Return address: <code>{escaped(redirect)}</code></p>
<form method="post" action="/oauth/authorize">
<input type="hidden" name="request" value="{escaped(request_id)}">
<label for="days">Access lasts</label><select id="days" name="days">{options}</select>
<small>You can revoke it at any time on the Agents page.</small>
<button type="submit" name="decision" value="allow">Allow read access</button>
<button type="submit" name="decision" value="deny" class="secondary">Deny</button>
</form></main></body></html>'''


def consent_policy(redirect):
    url = urlsplit(redirect)
    target = url.scheme + '://' + url.netloc if url.scheme == 'http' else url.scheme + ':'
    # form-action also covers the redirect after submission, so it names the callback.
    return ("default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self' " + target)


def message_page(title, text):
    escaped = html.escape
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Leti Cook · {escaped(title)}</title><link rel="stylesheet" href="/auth/assets/auth.css"></head>
<body><main><a class="brand" href="/app-explorer-v3" aria-label="Leti Cook">L</a><p class="eyebrow">AGENT ACCESS</p>
<h1>{escaped(title)}</h1><p>{escaped(text)}</p></main></body></html>'''


PAGE_POLICY = "default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
