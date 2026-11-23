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
from db.self_hosted_auth import (OwnerAuthRepository, AuthStoreError, SESSION_SECONDS, KNOWN_BROWSER_SECONDS,
                                 SignInAttempt)
from self_hosted_auth_protocol import HEADER, valid_worker_proof

PASSWORD_HASHER = PasswordHasher(time_cost=3,memory_cost=65536,parallelism=1,hash_len=32,salt_len=16)
ASSETS = Path(__file__).resolve().parent / 'self_hosted_ui'
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}\Z')
# The app pages a sign-in may return to (ownerSession.ts and auth.js check the same pattern): path and query only.
APP_RETURN = re.compile(r'/app-explorer-v3(?:/[A-Za-z0-9_~-][A-Za-z0-9._~-]*)*/?(?:\?[A-Za-z0-9._~%&=+:/-]*)?\Z')
PUBLIC_FILES = {'/auth':('index.html','text/html'),'/auth/assets/auth.js':('auth.js','text/javascript'),
                '/auth/assets/auth.css':('auth.css','text/css')}
# The owner is created only by /auth/claim then /auth/complete with a setup code from the terminal.
AUTH_PATHS = frozenset(('/auth/session','/auth/login','/auth/logout','/auth/claim','/auth/complete'))
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
            # The known-browser mark (db/self_hosted_auth.py): only sign-in reads it, so only its path receives it.
            self.known_cookie=('__Secure-lic-known-' if self.secure else 'lic-known-')+self.cookie[-12:]
            self.repository=repository or OwnerAuthRepository(os.environ['LIC_AUTH_DATABASE'],clock=clock)
            self.repository.configured()
        except (OSError,KeyError,ValueError,AuthStoreError):
            self.configuration_error=True
        self.worker_key=worker_key if worker_key is not None else os.getenv('LIC_HOT_RUNTIME_SECRET','')

    @staticmethod
    def reply(code,status=400):
        return JSONResponse({'code':code},status_code=status,headers={'Cache-Control':'no-store'})

    def agent_challenge(self,scope,receive,send):
        # RFC 6750 and RFC 9728: name the scheme and where agent programs find
        # how to sign in, so a client can start the owner's Allow flow.
        from agent_oauth import challenge_header
        return JSONResponse({'code':'agent_auth_required'},status_code=401,
                            headers={'Cache-Control':'no-store','WWW-Authenticate':challenge_header(self.origin)})(scope,receive,send)

    async def oauth_response(self,request):
        """Metadata, client registration and token exchange for agent programs."""
        import agent_oauth
        from db.agent_oauth import AgentOAuthRepository
        path,method=request.url.path,request.method
        if path in agent_oauth.METADATA_PATHS:
            if method!='GET':
                return self.reply('method_not_allowed',405)
            value=(agent_oauth.authorization_server_metadata(self.origin) if path.endswith('authorization-server')
                   else agent_oauth.protected_resource_metadata(self.origin))
            return JSONResponse(value,headers={'Cache-Control':'no-store'})
        if method!='POST':
            return self.reply('method_not_allowed',405)
        expected='application/json' if path=='/oauth/register' else 'application/x-www-form-urlencoded'
        if request.headers.get('content-type','').split(';')[0].strip().lower()!=expected:
            return JSONResponse({'error':'invalid_request'},status_code=400,headers={'Cache-Control':'no-store'})
        try:
            body=bytearray()
            async def read():
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body)>8192: raise ValueError('oauth_request_too_large')
            await asyncio.wait_for(read(),timeout=5)
            store=AgentOAuthRepository(self.repository.path)
            now=int(self.clock())
            if path=='/oauth/register':
                value=await run_in_threadpool(agent_oauth.register,store,bytes(body),now)
                return JSONResponse(value,status_code=201,headers={'Cache-Control':'no-store'})
            binding=await run_in_threadpool(self.repository.agent_binding)
            value=await run_in_threadpool(agent_oauth.token,store,bytes(body),binding,self.origin,now)
            return JSONResponse(value,headers={'Cache-Control':'no-store','Pragma':'no-cache'})
        except agent_oauth.OAuthError as error:
            answer={'error':error.code}
            if error.description: answer['error_description']=error.description
            return JSONResponse(answer,status_code=error.status,headers={'Cache-Control':'no-store'})
        except AuthStoreError as error:
            code=str(error)
            if code=='agent_client_rate':
                return JSONResponse({'error':'temporarily_unavailable','error_description':code},status_code=429,
                                    headers={'Cache-Control':'no-store','Retry-After':'60'})
            if code in ('agent_client_limit','agent_connection_limit'):
                return JSONResponse({'error':'temporarily_unavailable','error_description':code},status_code=503,
                                    headers={'Cache-Control':'no-store'})
            return JSONResponse({'error':'server_error'},status_code=503,headers={'Cache-Control':'no-store'})
        except (ValueError,OSError,asyncio.TimeoutError):
            return JSONResponse({'error':'invalid_request'},status_code=400,headers={'Cache-Control':'no-store'})

    def cookie_token(self,request):
        value=request.cookies.get(self.cookie,'')
        return value if TOKEN.fullmatch(value) else None

    def known_marks(self,request):
        # Every value of the mark's name, not the last one: on http://127.0.0.1 another
        # local site can add a cookie of that name, which must not hide the real mark.
        values=[]
        for header in request.headers.getlist('cookie'):
            for part in header.split(';'):
                name,_,value=part.strip().partition('=')
                if name==self.known_cookie and len(values)<4: values.append(value.strip())
        return values

    def remember_browser(self,response,attempt):
        if attempt.new_mark:
            response.set_cookie(self.known_cookie,attempt.new_mark,max_age=KNOWN_BROWSER_SECONDS,httponly=True,
                                secure=self.secure,samesite='strict',path='/auth/login')

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
        authorization = request.headers.getlist('authorization')
        # Agent programs discover, register and exchange codes without a session
        # or Origin; these exact endpoints read no library data (agent_oauth.py).
        from agent_oauth import PUBLIC_PATHS
        if path in PUBLIC_PATHS:
            if authorization:
                return await self.reply('authorization_not_accepted',400)(scope,receive,secure_send)
            return await (await self.oauth_response(request))(scope,receive,secure_send)
        # MCP clients are programs, not browsers, and send no Origin. /mcp takes
        # no cookie and always needs an agent key, so a missing Origin there is
        # not a cross-site request; a foreign Origin was refused above.
        mcp = path == '/mcp'
        if mcp and not authorization:
            return await self.agent_challenge(scope,receive,secure_send)
        if (request.method not in ('GET','HEAD','OPTIONS') and origin!=self.origin
                and not (mcp and origin is None)):
            return await self.reply('same_origin_required',403)(scope,receive,secure_send)
        # An explicit machine credential never falls back to a browser session
        # or trusted-local owner access, even when it is invalid or out of scope.
        if authorization:
            from agent_access import check, AGENT_ROUTES
            if len(authorization) != 1 or not authorization[0].startswith('Bearer '):
                return await self.agent_challenge(scope,receive,secure_send)
            agent_token = authorization[0][7:]
            from agent_oauth import resource_url
            resource = resource_url(self.origin)
            try:
                revision = await run_in_threadpool(lambda: check(self.repository, agent_token, clock=self.clock, resource=resource))
            except (AuthStoreError, OSError):
                return await self.reply('agent_access_unavailable',503)(scope,receive,secure_send)
            if revision is None:
                return await self.agent_challenge(scope,receive,secure_send)
            if (request.method, path) not in AGENT_ROUTES:
                return await self.reply('agent_scope_denied',403)(scope,receive,secure_send)
            def recheck_agent():
                try:
                    return check(self.repository, agent_token, clock=self.clock, resource=resource)
                except (AuthStoreError, OSError):
                    return None
            scope.setdefault('state',{}).update(research_access=True, access_mode='agent',
                research_access_recheck=recheck_agent)
            return await self.app(scope,receive,secure_send)
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
                    if request.method in ('GET','HEAD') and path=='/oauth/authorize':
                        # Sign in first, then return to the same Allow page (auth.js accepts only this path).
                        from urllib.parse import quote
                        query=scope.get('query_string',b'').decode('latin-1')
                        response=RedirectResponse('/auth?return='+quote(path+'?'+query,safe=''),status_code=303)
                    elif request.method in ('GET','HEAD') and ('text/html' in request.headers.get('accept','')):
                        # Sign in first, then return to the same app page (a reload after the session ended).
                        from urllib.parse import quote
                        query=scope.get('query_string',b'').decode('latin-1')
                        back=path+('?'+query if query else '')
                        response=RedirectResponse('/auth?return='+quote(back,safe='') if len(back)<=2048 and APP_RETURN.match(back) else '/auth',
                                                  status_code=303)
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
                    attempt=SignInAttempt()
                    result=await run_in_threadpool(self.repository.complete_setup,token_hash(token) if token else None,
                        mode,username,lambda:PASSWORD_HASHER.hash(data['password']),token_hash(new_token),now,attempt)
                # Setup codes are never counted: no refusal here asks to wait.
                if result!='ok':
                    return self.reply(result,409 if result=='setup_closed' else 401)
                response=JSONResponse({'status':'setup_verified' if path=='/auth/claim' else 'configured'})
                if path=='/auth/complete' and mode=='local':
                    response.delete_cookie(self.cookie,path='/',httponly=True,secure=self.secure,samesite='strict')
                else:
                    response.set_cookie(self.cookie,new_token,max_age=900 if path=='/auth/claim' else SESSION_SECONDS,
                        httponly=True,secure=self.secure,samesite='strict',path='/')
                    if path=='/auth/complete': self.remember_browser(response,attempt)
                return response
            if set(data)!={'username','password'}: raise ValueError('invalid_auth_request')
            username=normalize_username(data['username'])
            password=data['password']
            if not valid_password(password): raise ValueError('password_must_be_15_to_128_characters')
            new_token=secrets.token_urlsafe(32)
            attempt=SignInAttempt(self.known_marks(request))
            result=await run_in_threadpool(self.repository.login,
                lambda stored:hmac.compare_digest(stored,username),
                lambda encoded:verify_password(encoded,password),token_hash(new_token),now,
                token_hash(token) if token else None,attempt)
            if result!='ok':
                response=self.reply(result,429 if result=='rate_limited' else 401)
                # The wait is the refusal itself: nothing is held on the server meanwhile.
                if result=='rate_limited': response.headers['Retry-After']=str(attempt.retry_after)
                return response
            response=JSONResponse({'status':'authenticated'})
            response.set_cookie(self.cookie,new_token,max_age=SESSION_SECONDS,httponly=True,
                                secure=self.secure,samesite='strict',path='/')
            self.remember_browser(response,attempt)
            return response
        except (ValueError,UnicodeError,asyncio.TimeoutError):
            return self.reply('invalid_auth_request',400)
