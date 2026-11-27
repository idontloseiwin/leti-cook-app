"""Agent access over HTTP: the owner's key management and the read-only MCP endpoint.

Keys are created, listed and revoked only by the owner; an agent key never reaches
these routes. /mcp always needs an agent key and never falls back to owner access.
"""
import asyncio
import json
import os
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from research_mcp import (AUTHORIZATION, BATCH_SECONDS, MAX_BATCH, PROTOCOL_VERSIONS, ResearchCaller, ToolFailure,
                          accepts_json, batch_allowed, batch_errors, configured_origin, handle_batch,
                          handle_message, protocol_header_valid)

KEY_ERRORS = {'invalid_agent_key_options': 400, 'agent_key_limit': 409,
              'owner_access_not_configured': 409, 'agent_store_unavailable': 503}
NO_STORE = {'Cache-Control': 'no-store'}


def _owner(request):
    state = request.scope.get('state', {})
    return state.get('access_mode') != 'agent' and (state.get('installation_access') is True
                                                    or bool(state.get('owner_username')))


def _refused(code, status):
    return JSONResponse({'code': code}, status_code=status, headers=NO_STORE)


def _repositories():
    from db.agent_access import AgentAccessRepository
    from db.self_hosted_auth import OwnerAuthRepository
    owner = OwnerAuthRepository(os.environ['LIC_AUTH_DATABASE'])
    return owner, AgentAccessRepository(owner.path)


def create_agents_router():
    router = APIRouter()

    async def key_action(request, action):
        from db.self_hosted_auth import AuthStoreError
        if not _owner(request):
            return _refused('owner_access_required', 403)
        try:
            return JSONResponse(await run_in_threadpool(action), headers=NO_STORE)
        except AuthStoreError as error:
            code = str(error)
            return _refused(code if code in KEY_ERRORS else 'agent_store_unavailable', KEY_ERRORS.get(code, 503))
        except (OSError, KeyError):
            return _refused('agent_store_unavailable', 503)

    @router.get('/agent-setup/{client}', include_in_schema=False)
    async def agent_setup(client: str, request: Request):
        from agent_setup import download
        if not _owner(request):
            return _refused('owner_access_required', 403)
        try:
            name, body = await run_in_threadpool(download, client, configured_origin())
        except KeyError:
            return _refused('unknown_agent_program', 404)
        except ValueError:
            return _refused('local_installation_address_required', 409)
        return Response(body, media_type='application/octet-stream', headers={
            **NO_STORE, 'Content-Disposition': f'attachment; filename="{name}"', 'X-Content-Type-Options': 'nosniff'})

    @router.get('/agent-keys')
    async def list_agent_keys(request: Request):
        def action():
            from db.agent_access import MAX_KEYS
            from db.self_hosted_auth import AuthStoreError
            owner, store = _repositories()
            binding = owner.agent_binding()
            if not binding:
                raise AuthStoreError('owner_access_not_configured')
            # The store's own limit: the Agents page names it when a new key is refused.
            return {'keys': store.list(binding, int(time.time())), 'scope': 'research.read',
                    'mcp_url': configured_origin() + '/mcp', 'max_keys': MAX_KEYS}
        return await key_action(request, action)

    @router.post('/agent-keys')
    async def create_agent_key(request: Request):
        if not _owner(request):
            return _refused('owner_access_required', 403)
        try:
            data = json.loads(await request.body())
        except (ValueError, RecursionError):
            return _refused('invalid_agent_key_options', 400)
        if (type(data) is not dict or set(data) - {'name', 'days'} or type(data.get('name')) is not str
                or type(data.get('days', 30)) is not int):
            return _refused('invalid_agent_key_options', 400)

        def action():
            from agent_access import issue
            owner, _ = _repositories()
            metadata, secret = issue(owner, data['name'], data.get('days', 30))
            # The secret appears only in this response; only its hash is stored.
            return {'key': metadata, 'secret': secret}
        return await key_action(request, action)

    @router.delete('/agent-keys/{key_id}')
    async def revoke_agent_key(key_id: str, request: Request):
        try:
            identity = str(uuid.UUID(key_id))
        except ValueError:
            return _refused('invalid_agent_key_id', 400)

        def action():
            _, store = _repositories()
            return {'revoked': store.revoke(identity), 'id': identity}
        return await key_action(request, action)

    @router.get('/agent-connections')
    async def list_agent_connections(request: Request):
        def action():
            from db.agent_oauth import AgentOAuthRepository
            from db.self_hosted_auth import AuthStoreError
            owner, _ = _repositories()
            binding = owner.agent_binding()
            if not binding:
                raise AuthStoreError('owner_access_not_configured')
            return {'connections': AgentOAuthRepository(owner.path).list(binding, int(time.time())), 'max_connections': 32}
        return await key_action(request, action)

    @router.delete('/agent-connections/{connection_id}')
    async def revoke_agent_connection(connection_id: str, request: Request):
        try:
            identity = str(uuid.UUID(connection_id))
        except ValueError:
            return _refused('invalid_agent_connection_id', 400)

        def action():
            from db.agent_oauth import AgentOAuthRepository
            owner, _ = _repositories()
            return {'revoked': AgentOAuthRepository(owner.path).revoke(identity), 'id': identity}
        return await key_action(request, action)

    @router.get('/oauth/authorize')
    async def authorize_page(request: Request):
        import agent_oauth
        from urllib.parse import parse_qs
        from db.agent_oauth import AgentOAuthRepository
        from db.self_hosted_auth import AuthStoreError
        if not _owner(request):
            return _page('Owner access required', 'Sign in to Leti Cook in this browser, then open the link again.', 403)
        origin = configured_origin()
        try:
            params = parse_qs(request.url.query, keep_blank_values=True, max_num_fields=20)
        except ValueError:
            return _page('This request cannot be used', 'The agent program sent too many parameters.', 400)

        def prepare():
            owner, _ = _repositories()
            store = AgentOAuthRepository(owner.path)
            return agent_oauth.authorization_request(store, params, origin, int(time.time()))
        try:
            pending, client, redirect = await run_in_threadpool(prepare)
        except agent_oauth.RedirectError as error:
            return Response(status_code=302, headers={**NO_STORE, 'Location': agent_oauth.error_redirect(
                error.redirect, error.state, error.code, origin)})
        except agent_oauth.OAuthError as error:
            return _page('This request cannot be used', 'The agent program sent an unknown or changed request ('
                         + error.code + '). Start the connection again from the program.', 400)
        except (AuthStoreError, OSError, KeyError):
            return _page('Agent access is unavailable', 'The agent connection store cannot be read right now.', 503)
        return Response(agent_oauth.consent_page(pending, client, redirect, origin), media_type='text/html',
                        headers={**NO_STORE, 'Content-Security-Policy': agent_oauth.consent_policy(redirect)})

    @router.post('/oauth/authorize')
    async def authorize_decision(request: Request):
        import agent_oauth
        from urllib.parse import parse_qs
        from db.agent_oauth import AgentOAuthRepository
        from db.self_hosted_auth import AuthStoreError
        if not _owner(request):
            return _page('Owner access required', 'Sign in to Leti Cook in this browser, then start again.', 403)
        if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/x-www-form-urlencoded':
            return _page('This request cannot be used', 'Unexpected form submission.', 400)
        try:
            form = parse_qs((await request.body()).decode('ascii'), max_num_fields=5)
            if any(len(values) != 1 for values in form.values()) or set(form) - {'request', 'decision', 'days'}:
                raise ValueError
            decision, days = form['decision'][0], int(form.get('days', [str(agent_oauth.DEFAULT_DAYS)])[0])
            if decision not in ('allow', 'deny'):
                raise ValueError
        except (KeyError, ValueError, UnicodeError):
            return _page('This request cannot be used', 'Unexpected form submission.', 400)

        def finish():
            owner, _ = _repositories()
            return agent_oauth.decide(AgentOAuthRepository(owner.path), form['request'][0], decision == 'allow', days,
                                      owner.agent_binding(), configured_origin(), int(time.time()))
        try:
            target = await run_in_threadpool(finish)
        except agent_oauth.OAuthError as error:
            return _page('This request has expired' if error.code == 'agent_request_expired' else 'This request cannot be used',
                         'Start the connection again from the agent program.', 400)
        except (AuthStoreError, OSError, KeyError):
            return _page('Agent access is unavailable', 'The agent connection store cannot be written right now.', 503)
        return Response(status_code=303, headers={**NO_STORE, 'Location': target})

    @router.post('/mcp')
    async def mcp_message(request: Request):
        authorization = request.headers.get('authorization', '')
        if request.scope.get('state', {}).get('access_mode') != 'agent' or not AUTHORIZATION.fullmatch(authorization):
            return JSONResponse({'code': 'agent_auth_required'}, status_code=401,
                                headers={**NO_STORE, 'WWW-Authenticate': 'Bearer'})
        if not protocol_header_valid(request.headers.get('mcp-protocol-version')):
            return _rpc_error(400, -32600, 'unsupported_protocol_version')
        if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
            return _rpc_error(415, -32700, 'content_type_must_be_application_json')
        if not accepts_json(request.headers.get('accept')):
            return _rpc_error(406, -32600, 'accept_application_json_required')
        try:
            message = json.loads(await request.body())
        except (ValueError, RecursionError):
            return _rpc_error(400, -32700, 'parse_error')
        except ClientDisconnect:
            # A client may abandon an in-flight message when it closes; nothing to answer.
            return Response(status_code=400, headers=NO_STORE)
        caller = ResearchCaller(request.app, configured_origin(), authorization)
        if type(message) is list:
            if not batch_allowed(request.headers.get('mcp-protocol-version')):
                return _rpc_error(400, -32600, 'batch_not_supported')
            if not 1 <= len(message) <= MAX_BATCH:
                return _rpc_error(400, -32600, 'invalid_batch')
            async def access_check():
                check = getattr(request.state, 'research_access_recheck', None)
                return callable(check) and await run_in_threadpool(check) is not None

            try:
                async with asyncio.timeout(BATCH_SECONDS):
                    answers = await handle_batch(message, caller, access_check)
            except TimeoutError:
                answers = batch_errors(message, 'batch_time_budget_exceeded_reduce_calls')
            except ToolFailure as error:
                answers = batch_errors(message, str(error))
            # No buffered data has left the process yet. Revoke applies to this
            # complete response, including earlier successful elements.
            if not await access_check():
                from agent_oauth import challenge_header
                return JSONResponse({'code': 'agent_key_invalid_expired_or_revoked'}, status_code=401,
                                    headers={**NO_STORE, 'WWW-Authenticate': challenge_header(configured_origin())})
            return JSONResponse(answers, headers=NO_STORE) if answers else Response(status_code=202, headers=NO_STORE)
        answer = await handle_message(message, caller)
        if answer is None:
            return Response(status_code=202, headers=NO_STORE)
        invalid = 'error' in answer and answer['error']['code'] in (-32600, -32700)
        return JSONResponse(answer, status_code=400 if invalid else 200, headers=NO_STORE)

    async def no_stream(request: Request):
        # No server-initiated stream and no session to delete (stateless server).
        return JSONResponse({'code': 'method_not_allowed', 'protocol_versions': list(PROTOCOL_VERSIONS)},
                            status_code=405, headers={**NO_STORE, 'Allow': 'POST'})

    router.add_api_route('/mcp', no_stream, methods=['GET', 'DELETE'], include_in_schema=False)
    return router


def _page(title, text, status):
    from agent_oauth import PAGE_POLICY, message_page
    return Response(message_page(title, text), status_code=status, media_type='text/html',
                    headers={**NO_STORE, 'Content-Security-Policy': PAGE_POLICY})


def _rpc_error(status, code, message):
    return JSONResponse({'jsonrpc': '2.0', 'id': None, 'error': {'code': code, 'message': message}},
                        status_code=status, headers=NO_STORE)
