"""Bounded loopback HTTP transport for the stdio bridge. Standard library only.

No browser session, redirect, proxy, cookie or YouTube access; the key is reread
from its private file for every message and never printed.
"""
import json
import os
from pathlib import Path
import re
import stat
import urllib.error
import urllib.request
from urllib.parse import urlsplit

TOKEN = re.compile(r'lic_ro_[A-Za-z0-9_-]{43}\Z')
MAX_MESSAGE = 128 * 1024
MAX_RESPONSE = 6 * 1024 * 1024
TIMEOUT_SECONDS = 45
STATUS_MESSAGES = {
    401: 'agent_key_invalid_expired_or_revoked', 403: 'access_denied_check_key_and_base_url',
    404: 'mcp_endpoint_not_available_update_application', 405: 'mcp_method_not_allowed',
    409: 'installation_setup_required', 413: 'message_too_large', 503: 'application_not_ready_retry_later',
}


class ApiError(Exception):
    """Only closed, safe messages cross the protocol boundary."""


def token_from_file(filename):
    path = Path(filename).absolute()
    try:
        for item in (path, *path.parents):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 1024:
                raise ApiError('credential_file_must_not_be_a_link')
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 256:
            raise ApiError('invalid_credential_file')
        if os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ApiError('credential_file_must_be_private')
        token = path.read_text(encoding='ascii').strip()
        if not TOKEN.fullmatch(token):
            raise ApiError('invalid_credential_file')
        return token
    except (OSError, UnicodeError):
        raise ApiError('credential_file_unavailable') from None


def origin(value):
    try:
        url = urlsplit(value)
        if (url.scheme not in ('http', 'https') or url.hostname not in ('127.0.0.1', 'localhost', '::1')
                or url.username is not None or url.password is not None or url.path not in ('', '/')
                or url.query or url.fragment or not url.port or not 1 <= url.port <= 65535):
            raise ValueError()
        host = '[::1]' if url.hostname == '::1' else url.hostname
        return f'{url.scheme}://{host}:{url.port}'
    except ValueError:
        raise ApiError('base_url_must_be_a_loopback_origin_with_port') from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # A redirect would carry the key elsewhere: answer it as an error.


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class Transport:
    def __init__(self, base_url, token_file, *, opener=None):
        self.url, self.token_file = origin(base_url) + '/mcp', token_file
        self.opener = opener or _OPENER  # Test injection only.

    def post(self, message, protocol_version=None):
        """Send one JSON-RPC message; return the decoded response, or None for 202."""
        body = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
        if len(body) > MAX_MESSAGE:
            raise ApiError('message_too_large')
        headers = {'Authorization': 'Bearer ' + token_from_file(self.token_file),
                   'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
        if protocol_version:
            headers['MCP-Protocol-Version'] = protocol_version
        request = urllib.request.Request(self.url, data=body, headers=headers, method='POST')
        try:
            with self.opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                status, kind, raw = response.status, response.headers.get('Content-Type', ''), response.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as error:
            status, kind = error.code, error.headers.get('Content-Type', '') if error.headers else ''
            raw = error.read(MAX_RESPONSE + 1) if error.fp else b''
        except (urllib.error.URLError, OSError, ValueError):
            raise ApiError('application_unreachable_check_base_url_and_tunnel') from None
        if len(raw) > MAX_RESPONSE:
            raise ApiError('response_too_large_reduce_page_or_columns')
        if status == 202:
            return None
        value = None
        if kind.split(';')[0].strip().lower() == 'application/json':
            try:
                value = json.loads(raw)
            except (ValueError, RecursionError):
                value = None
        if status in (200, 400) and type(value) is dict and value.get('jsonrpc') == '2.0':
            return value
        if (status == 200 and type(message) is list and type(value) is list and value
                and all(type(item) is dict and item.get('jsonrpc') == '2.0' and 'id' in item
                        and (('result' in item) != ('error' in item)) for item in value)):
            return value
        code = value.get('code') if type(value) is dict else None
        if 300 <= status < 400 or code in ('invalid_host', 'cross_origin_denied', 'same_origin_required'):
            raise ApiError('base_url_must_match_installation_origin')
        raise ApiError(STATUS_MESSAGES.get(status, 'request_failed'))
