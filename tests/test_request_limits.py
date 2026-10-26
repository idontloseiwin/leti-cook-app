"""Request bodies and request strings are bounded. Plain ASGI and pydantic only: no server or database."""
import asyncio
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
import request_limits
from request_limits import RequestBodyLimitMiddleware, RequestBodyTooLarge

def run(middleware,path,chunks,*,content_length=None,start_before_reading=False):
    """Drive one HTTP request through the middleware; returns (status, body, bytes the app read, app was called)."""
    state={'read':0,'called':False}
    async def app(scope,receive,send):
        state['called']=True
        if start_before_reading:
            await send({'type':'http.response.start','status':200,'headers':[]})
        while True:
            message=await receive()
            state['read']+=len(message.get('body',b''))
            if not message.get('more_body'):break
        if not start_before_reading:
            await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':b'ok'})
    messages=[{'type':'http.request','body':chunk,'more_body':index<len(chunks)-1} for index,chunk in enumerate(chunks)]
    async def receive():
        return messages.pop(0)
    sent=[]
    async def send(message):sent.append(message)
    headers=[(b'content-length',str(content_length).encode())] if content_length is not None else []
    asyncio.run(middleware(app)({'type':'http','path':path,'headers':headers},receive,send))
    status=next(m['status'] for m in sent if m['type']=='http.response.start')
    return status,b''.join(m.get('body',b'') for m in sent if m['type']=='http.response.body'),state['read'],state['called']

def small(app):
    return RequestBodyLimitMiddleware(app,default_limit=10,path_limits={'/user-work/restore':100})

class BodyLimitTests(unittest.TestCase):
    def test_bodies_within_the_limit_reach_the_route(self):
        self.assertEqual(run(small,'/folders',[b'12345',b'67890'],content_length=10)[:3],(200,b'ok',10))

    def test_declared_oversized_body_is_refused_before_the_route_runs(self):
        status,body,_,called=run(small,'/folders',[b'x'*11],content_length=11)
        self.assertEqual((status,called),(413,False))
        self.assertIn(b'request_too_large',body)

    def test_streamed_oversized_body_is_refused(self):
        status,_,read,_=run(small,'/folders',[b'x'*6,b'x'*6,b'x'*6])
        self.assertEqual(status,413)
        self.assertLessEqual(read,10)

    def test_backup_restore_allows_its_larger_limit(self):
        self.assertEqual(run(small,'/user-work/restore',[b'x'*60],content_length=60)[0],200)
        self.assertEqual(run(small,'/user-work/restore',[b'x'*101],content_length=101)[0],413)

    def test_an_inner_error_answer_becomes_413(self):
        # FastAPI turns a failed body read on a model route into 400 "error parsing the body".
        reads=[]
        async def app(scope,receive,send):
            try:
                while (await receive()).get('more_body'):pass
            except RequestBodyTooLarge:
                try:
                    await receive()
                except RequestBodyTooLarge:
                    reads.append('refused again')
                await send({'type':'http.response.start','status':400,'headers':[]})
                await send({'type':'http.response.body','body':b'{"detail":"There was an error parsing the body"}'})
        messages=[{'type':'http.request','body':b'x'*6,'more_body':True} for _ in range(3)]
        async def receive():return messages.pop(0)
        sent=[]
        async def send(message):sent.append(message)
        asyncio.run(small(app)({'type':'http','path':'/folders','headers':[]},receive,send))
        self.assertEqual([m.get('status') for m in sent if m['type']=='http.response.start'],[413])
        self.assertEqual(b''.join(m.get('body',b'') for m in sent if m['type']=='http.response.body'),request_limits.TOO_LARGE)
        self.assertEqual(reads,['refused again'])

    def test_a_started_response_is_not_replaced(self):
        with self.assertRaises(RequestBodyTooLarge):
            run(small,'/folders',[b'x'*6,b'x'*6],start_before_reading=True)

    def test_other_scopes_pass_through(self):
        seen=[]
        async def app(scope,receive,send):seen.append(scope['type'])
        asyncio.run(RequestBodyLimitMiddleware(app)({'type':'lifespan'},None,None))
        self.assertEqual(seen,['lifespan'])

    def test_limits_leave_room_for_the_routes_own_caps(self):
        import user_work_backup, saved_work, video_type_reviews
        # Each route's own check fires first, with its own error code.
        self.assertGreater(request_limits.BACKUP_BODY_LIMIT,user_work_backup.MAX_BYTES)
        for own in (saved_work.MAX_BODY_BYTES,video_type_reviews.MAX_BODY_BYTES,4096,8192):
            self.assertLess(own,request_limits.DEFAULT_BODY_LIMIT)
        self.assertEqual(set(request_limits.PATH_BODY_LIMITS),{'/user-work/restore','/user-work/restore/preview',
            '/research/queries/validate','/research/queries/execute'})
        for path in ('/research/queries/validate','/research/queries/execute'):
            self.assertEqual(request_limits.PATH_BODY_LIMITS[path],64*1024)

    def test_registered_inside_owner_access(self):
        source=(ROOT/'application/server.py').read_text(encoding='utf-8')
        limit=source.index('app.add_middleware(RequestBodyLimitMiddleware)')
        self.assertLess(limit,source.index('app.add_middleware(SelfHostedAuthMiddleware)'))
        self.assertEqual(source.count('app.add_middleware(RequestBodyLimitMiddleware)'),1)
        files=(ROOT/'packaging/files.json').read_text(encoding='utf-8')
        self.assertIn('"application/request_limits.py": "application/request_limits.py"',files)

class RequestModelTests(unittest.TestCase):
    def models(self):
        from pydantic import ValidationError
        from web.models import ChannelRequest, FolderRequest, FolderAssign, LabelCreateRequest
        return {model.__name__: model for model in
                (ChannelRequest, FolderRequest, FolderAssign, LabelCreateRequest)}, ValidationError

    def test_strings_are_bounded(self):
        models,ValidationError=self.models()
        for name,field,maximum in (('ChannelRequest','url',2048),('FolderRequest','name',80),('FolderAssign','folder_id',64),
                                   ('LabelCreateRequest','name',200)):
            self.assertEqual(getattr(models[name](**{field:'x'*maximum}),field),'x'*maximum)
            with self.assertRaises(ValidationError,msg=name):
                models[name](**{field:'x'*(maximum+1)})

    def test_folder_limit_matches_the_folder_form(self):
        form=(ROOT/'application/frontend/src/explorer/ExplorerWorkspace.tsx').read_text(encoding='utf-8')
        self.assertIn('maxLength={80}',form)

if __name__=='__main__':unittest.main()
