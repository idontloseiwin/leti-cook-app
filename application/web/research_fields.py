"""Owner-only field settings; policy writes do not call YouTube."""
import json
import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from db.base.postgres import DatabaseError
from db.user_work.research_preferences import ResearchPreferencesRepository
from db.hot.hot_retention import HotRetentionRepository, RetentionError
from research_field_policy import FieldPolicyError, DEFINITIONS, FIELDS, TYPES
from research_field_settings import preview
from dataclasses import asdict
from saved_work import validate_mutation_headers, SavedWorkError


def create_field_router(database, authorize_owner):
    router=APIRouter(prefix='/fields',dependencies=[Depends(authorize_owner)])
    repository=ResearchPreferencesRepository(database)
    def check(request):
        # No catalogue switch; ended owner access still refuses before and after the work.
        if request.state.owner_access_recheck() is None:raise FieldPolicyError('policy_denied',403)
    async def body(request,keys):
        try:validate_mutation_headers(request.headers,request.url.scheme)
        except SavedWorkError:raise FieldPolicyError('cross_origin',403) from None
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>32768:raise FieldPolicyError('field_preferences_too_large',413)
        try:value=json.loads(raw)
        except (ValueError,UnicodeError,RecursionError):raise FieldPolicyError() from None
        if type(value) is not dict or set(value)!=set(keys):raise FieldPolicyError()
        return value
    async def call(request,fn):
        def work():
            check(request);value=fn();check(request);return value
        try:return JSONResponse(await anyio.to_thread.run_sync(work),headers={'Cache-Control':'no-store'})
        except FieldPolicyError as error:return JSONResponse({'code':error.code},status_code=error.status,headers={'Cache-Control':'no-store'})
        except DatabaseError:return JSONResponse({'code':'field_preferences_unavailable'},status_code=503,headers={'Cache-Control':'no-store'})
    @router.get('')
    async def get(request:Request):
        return await call(request,lambda:dict(preferences=repository.read(),fields=[asdict(field) for field in DEFINITIONS],
            fixed_dependencies=['identities','typed membership','availability','publication','source clocks','expiry'],
            existing_fields='Other current fields preserve existing collection behavior; independent storage toggles are not offered.'))
    @router.post('/preview')
    async def examine(request:Request):
        try:command=await body(request,('document',))
        except FieldPolicyError as error:return JSONResponse({'code':error.code},status_code=error.status)
        return await call(request,lambda:preview(repository,command['document']))
    @router.post('/apply')
    async def apply(request:Request):
        try:command=await body(request,('document','expected_revision','request_id','purge'))
        except FieldPolicyError as error:return JSONResponse({'code':error.code},status_code=error.status)
        def work():
            result=repository.apply(command['document'],command['expected_revision'],command['request_id'],command['purge'])
            # Policy commits first. An uncertain cleanup outcome never replays
            # the command blindly; retained data is hidden and cleanup retries.
            progress=[]
            for field in result['preferences']['purge_fields']:
                try:
                    page=HotRetentionRepository(database).cleanup_page('video_description' if field=='video.description' else 'video_paid_promotion',limit=100)
                    progress.append({'field':field,'scrubbed':page.scrubbed,'complete':page.complete})
                except (DatabaseError,RetentionError):progress.append({'field':field,'complete':False})
            result['purge_progress']=progress;return result
        return await call(request,work)
    return router
