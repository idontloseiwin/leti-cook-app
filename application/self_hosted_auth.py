"""Single-owner self-hosted HTTP boundary. No provider calls or API-data reads."""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from db.self_hosted_auth import OwnerAuthRepository, AuthStoreError, SESSION_SECONDS
from self_hosted_auth_protocol import HEADER, valid_worker_proof

PASSWORD_HASHER = PasswordHasher(time_cost=3,memory_cost=65536,parallelism=1,hash_len=32,salt_len=16)
ASSETS = Path(__file__).resolve().parent / 'self_hosted_ui'
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}\Z')
PUBLIC_FILES = {'/auth':('index.html','text/html'),'/auth/assets/auth.js':('auth.js','text/javascript'),
                '/auth/assets/auth.css':('auth.css','text/css')}
AUTH_PATHS = frozenset(('/auth/session','/auth/register','/auth/login','/auth/logout','/auth/claim','/auth/complete'))
# Stored thumbnails (img.youtube.com, video_thumbnails.py) and channel avatars;
# frontend/src/explorer/imageHosts.ts admits the same hosts.
IMAGE_HOSTS = ('img.youtube.com','i.ytimg.com','yt3.ggpht.com','yt3.googleusercontent.com')
# Every response without its own policy: the application uses only its own
# scripts, styles, fonts and API. data: images are the embedded YouTube mark.
APPLICATION_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; "
                   "img-src 'self' data: "+' '.join('https://'+host for host in IMAGE_HOSTS)+"; "
                   "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'").encode()


def token_hash(value):
    return hashlib.sha256(b'lic-owner-token-v1\0'+value.encode()).hexdigest()


def valid_password(value):
    return isinstance(value,str) and 15 <= len(value) <= 128 and '\0' not in value


def normalize_username(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{2,63}',value):
        raise ValueError('invalid_username')
    return value.lower()


def verify_password(encoded,value):
    try:
        return PASSWORD_HASHER.verify(encoded,value)
    except (VerificationError,InvalidHashError):
        return False


def public_origin(value):
    parsed=urlsplit(value or '')
    try: port=parsed.port
    except ValueError: raise ValueError('invalid_auth_origin') from None
    if (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment or (port is not None and not 1 <= port <= 65535)):
        raise ValueError('invalid_auth_origin')
    if parsed.scheme=='http' and parsed.hostname not in ('127.0.0.1','localhost','::1'):
        raise ValueError('http_auth_requires_loopback_origin')
    return value


def loopback_alias(host,origin):
    """Another loopback name for a loopback http origin on the same port, e.g. localhost:8002."""
    parsed=urlsplit(origin)
    if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost','::1'):
        return False
    name,separator,port=host.rpartition(':')
    if not separator or ']' in port: name,port=host,'80'
    return (host!=parsed.netloc and port==str(parsed.port or 80)
            and name.lower() in ('127.0.0.1','localhost','[::1]'))


class SelfHostedAuthMiddleware:
    """Outermost boundary: auth UI works while hot storage remains unavailable.

    Mandatory in the self-hosted profile; local access requires a consumed setup code.
    Unconfigured/missing/corrupt auth state fails closed. All unlisted paths are
    protected, including future routes, docs, telemetry and old command endpoints.
    """
    def __init__(self,app,*,repository=None,origin=None,worker_key=None,clock=time.time):
        self.app,self.clock=app,clock
        self.repository=repository
        self.configuration_error=False
        try:
            self.origin=public_origin(origin if origin is not None else os.getenv('LIC_PUBLIC_ORIGIN'))
            self.host=urlsplit(self.origin).netloc
            self.secure=self.origin.startswith('https:')
            self.cookie=('__Host-lic-session-' if self.secure else 'lic-session-')+hashlib.sha256(self.origin.encode()).hexdigest()[:12]
            self.repository=repository or OwnerAuthRepository(os.environ['LIC_AUTH_DATABASE'],clock=clock)
            self.repository.configured()
        except (OSError,KeyError,ValueError,AuthStoreError):
            self.configuration_error=True
        self.worker_key=worker_key if worker_key is not None else os.getenv('LIC_HOT_RUNTIME_SECRET','')

    @staticmethod
    def reply(code,status=400):
        return JSONResponse({'code':code},status_code=status,headers={'Cache-Control':'no-store'})

    def cookie_token(self,request):
        value=request.cookies.get(self.cookie,'')
        return value if TOKEN.fullmatch(value) else None

    def access_recheck(self, mode, token=None):
        """Recheck a bounded read at completion without extending its session."""
        identity = token_hash(token) if token else None
        def check():
            try:
                now = int(self.clock())
                if self.repository.access_state(now)['mode'] != mode:
                    return None
                if mode == 'local':
                    return 'owner.local'
                if identity and self.repository.session(identity, now, touch=False):
                    return 'owner.' + identity
            except (AuthStoreError, OSError):
                pass
            return None
        return check

    async def __call__(self,scope,receive,send):
        if scope['type']=='lifespan': return await self.app(scope,receive,send)
        if scope['type']!='http':
            return await send({'type':'websocket.close','code':4401})
        request=Request(scope,receive)
        path=scope.get('path','')
        async def secure_send(message):
            if message['type']=='http.response.start':
                headers=[(k,v) for k,v in message['headers'] if k.lower() not in
                         (b'access-control-allow-origin',b'access-control-allow-credentials',b'cache-control')]
                headers.extend([(b'cache-control',b'no-store'),(b'x-content-type-options',b'nosniff'),
                                (b'x-frame-options',b'DENY'),(b'referrer-policy',b'no-referrer')])
                if path in PUBLIC_FILES:
                    headers.append((b'content-security-policy',b"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"))
                elif not any(k.lower()==b'content-security-policy' for k,_ in headers):
                    # Setup and data-check pages keep their stricter policies.
                    headers.append((b'content-security-policy',APPLICATION_CSP))
                message={**message,'headers':headers}
            await send(message)
        # Exact machine endpoints use independent path-bound HMAC authorization,
        # never a session-cookie exception for a whole /internal prefix.
        if path in ('/internal/hot-runtime', '/internal/update-status'):
            from update_runtime_protocol import valid_request_proof
            validate_proof = valid_worker_proof if path == '/internal/hot-runtime' else valid_request_proof
            peer=(scope.get('client') or ('',0))[0]
            if (request.method=='GET' and peer in ('127.0.0.1','::1')
                    and validate_proof(request.headers.get(HEADER),self.worker_key,now=self.clock())):
                return await self.app(scope,receive,secure_send)
            return await self.reply('internal_auth_required',403)(scope,receive,secure_send)
        if self.configuration_error:
            return await self.reply('owner_access_not_configured',503)(scope,receive,secure_send)
        # One explicitly configured origin also prevents DNS rebinding. Proxy
        # headers and arbitrary reflected Host values do not grant trust.
        hosts=request.headers.getlist('host')
        if hosts != [self.host]:
            # A browser on this machine may open localhost instead of 127.0.0.1.
            # Send it to the configured origin; the redirect serves no content.
            raw=(scope.get('raw_path') or path.encode()).decode('latin-1')
            if request.method in ('GET','HEAD') and len(hosts)==1 and raw.startswith('/') and loopback_alias(hosts[0],self.origin):
                query=scope.get('query_string',b'').decode('latin-1')
                return await RedirectResponse(self.origin+raw+('?'+query if query else ''),status_code=307)(scope,receive,secure_send)
            return await self.reply('invalid_host',400)(scope,receive,secure_send)
        origin=request.headers.get('origin')
        if (origin is not None and origin!=self.origin) or request.headers.get('sec-fetch-site') in ('cross-site','same-site'):
            return await self.reply('cross_origin_denied',403)(scope,receive,secure_send)
        if request.method not in ('GET','HEAD','OPTIONS') and origin!=self.origin:
            return await self.reply('same_origin_required',403)(scope,receive,secure_send)
        try:
            if path in PUBLIC_FILES and request.method in ('GET','HEAD'):
                name,media=PUBLIC_FILES[path]
                response=FileResponse(ASSETS/name,media_type=media)
            elif path in AUTH_PATHS:
                response=await self.auth_response(request)
            else:
                token=self.cookie_token(request)
                state=await run_in_threadpool(self.repository.access_state,int(self.clock()))
                if state['mode']=='local':
                    if urlsplit(self.origin).hostname not in ('127.0.0.1','localhost','::1'):
                        return await self.reply('local_access_requires_loopback',403)(scope,receive,secure_send)
                    scope.setdefault('state',{}).update(installation_access=True,access_mode='local',
                        owner_access_recheck=self.access_recheck('local'))
                    return await self.app(scope,receive,secure_send)
                owner=await run_in_threadpool(self.repository.session,token_hash(token),int(self.clock())) if token else None
                if not owner:
                    if request.method in ('GET','HEAD') and ('text/html' in request.headers.get('accept','')):
                        response=RedirectResponse('/auth',status_code=303)
                    else:
                        response=self.reply('authentication_required',401)
                else:
                    scope.setdefault('state',{}).update(owner_username=owner['username'],installation_access=True,access_mode='password',
                        owner_access_recheck=self.access_recheck('password', token))
                    return await self.app(scope,receive,secure_send)
        except (AuthStoreError,OSError):
            response=self.reply('owner_access_unavailable',503)
        return await response(scope,receive,secure_send)

    async def body(self,request):
        if request.headers.get('content-type','').split(';')[0]!='application/json' or request.headers.get('x-lic-auth')!='1':
            raise ValueError('json_auth_request_required')
        chunks=bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks)>4096: raise ValueError('auth_request_too_large')
        def unique(pairs):
            result={}
            for key,value in pairs:
                if key in result: raise ValueError('invalid_auth_request')
                result[key]=value
            return result
        value=json.loads(chunks,object_pairs_hook=unique)
        if not isinstance(value,dict): raise ValueError('invalid_auth_request')
        return value

    async def auth_response(self,request):
        path,now=request.url.path,int(self.clock())
        token=self.cookie_token(request)
        if path=='/auth/session' and request.method=='GET':
            owner=await run_in_threadpool(self.repository.session,token_hash(token),now,touch=False) if token else None
            state=await run_in_threadpool(self.repository.access_state,now,token_hash(token) if token else None)
            local=state['mode']=='local' and urlsplit(self.origin).hostname in ('127.0.0.1','localhost','::1')
            return JSONResponse({'configured':state['mode']!='pending','authenticated':bool(owner) or local,
                                 'username':owner['username'] if owner else None,'access_mode':state['mode'],
                                 'setup_verified':state['setup_verified'],'setup_expired':state['setup_expired']})
        if request.method!='POST': return self.reply('method_not_allowed',405)
        try:
            data=await asyncio.wait_for(self.body(request),timeout=5)
            if path=='/auth/logout':
                if data: raise ValueError('invalid_auth_request')
                if token: await run_in_threadpool(self.repository.logout,token_hash(token))
                response=JSONResponse({'status':'signed_out'})
                response.delete_cookie(self.cookie,path='/',httponly=True,secure=self.secure,samesite='strict')
                return response
            if path in ('/auth/claim','/auth/complete'):
                new_token=secrets.token_urlsafe(32)
                if path=='/auth/claim':
                    if set(data)!={'setup_code'} or not isinstance(data['setup_code'],str) or not TOKEN.fullmatch(data['setup_code']):
                        raise ValueError('invalid_setup_code')
                    result=await run_in_threadpool(self.repository.claim_setup,
                        lambda encoded:hmac.compare_digest(encoded,token_hash(data['setup_code'])),token_hash(new_token),now)
                else:
                    mode=data.get('mode')
                    if mode=='local':
                        if set(data)!={'mode'}: raise ValueError('invalid_auth_request')
                        if urlsplit(self.origin).hostname not in ('127.0.0.1','localhost','::1'):
                            return self.reply('local_access_requires_loopback',403)
                        username=None
                    elif mode=='password':
                        if set(data)!={'mode','username','password'}: raise ValueError('invalid_auth_request')
                        username=normalize_username(data['username'])
                        if not valid_password(data['password']): raise ValueError('invalid_auth_request')
                    else: raise ValueError('invalid_auth_request')
                    result=await run_in_threadpool(self.repository.complete_setup,token_hash(token) if token else None,
                        mode,username,lambda:PASSWORD_HASHER.hash(data['password']),token_hash(new_token),now)
                if result!='ok':
                    response=self.reply(result,429 if result=='rate_limited' else 409 if result=='setup_closed' else 401)
                    if result=='rate_limited': response.headers['Retry-After']='60'
                    return response
                response=JSONResponse({'status':'setup_verified' if path=='/auth/claim' else 'configured'})
                if path=='/auth/complete' and mode=='local':
                    response.delete_cookie(self.cookie,path='/',httponly=True,secure=self.secure,samesite='strict')
                else:
                    response.set_cookie(self.cookie,new_token,max_age=900 if path=='/auth/claim' else SESSION_SECONDS,
                        httponly=True,secure=self.secure,samesite='strict',path='/')
                return response
            expected={'username','password','setup_code'} if path=='/auth/register' else {'username','password'}
            if set(data)!=expected: raise ValueError('invalid_auth_request')
            username=normalize_username(data['username'])
            password=data['password']
            if not valid_password(password): raise ValueError('password_must_be_15_to_128_characters')
            new_token=secrets.token_urlsafe(32)
            if path=='/auth/register':
                code=data['setup_code']
                if not isinstance(code,str) or not TOKEN.fullmatch(code): raise ValueError('invalid_setup_code')
                result=await run_in_threadpool(self.repository.register,username,
                    lambda encoded:hmac.compare_digest(encoded,token_hash(code)),
                    lambda:PASSWORD_HASHER.hash(password),token_hash(new_token),now)
            else:
                result=await run_in_threadpool(self.repository.login,
                    lambda stored:hmac.compare_digest(stored,username),
                    lambda encoded:verify_password(encoded,password),token_hash(new_token),now,
                    token_hash(token) if token else None)
            if result!='ok':
                response=self.reply(result,429 if result=='rate_limited' else 409 if result=='setup_closed' else 401)
                if result=='rate_limited': response.headers['Retry-After']='60'
                return response
            response=JSONResponse({'status':'authenticated'})
            response.set_cookie(self.cookie,new_token,max_age=SESSION_SECONDS,httponly=True,
                                secure=self.secure,samesite='strict',path='/')
            return response
        except (ValueError,UnicodeError,asyncio.TimeoutError):
            return self.reply('invalid_auth_request',400)
