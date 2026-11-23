"""Writes past a user-work backup limit are refused with a clear message. No database or provider."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from http_test_support import cache_stub, library_stub, endpoint
from web.organization import create_organization_router
from web.models import FolderRequest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from saved_work import SavedObjectService, SavedWorkError  # noqa: E402
from user_work_backup import UserWorkError  # noqa: E402
from user_work_service import UserWorkService  # noqa: E402
from video_type_reviews import ReviewError, VideoTypeReviewService  # noqa: E402

SCHEMA=(ROOT/'schema/schema.sql').read_bytes().decode('utf-8').replace('\r\n','\n')

class LimitError(Exception):
    """What the database client raises for the enforce_user_work_backup_limits trigger."""
    message='user_work_limit_reached'

def refuse(*args, **kwargs):
    raise LimitError()

class ServiceMappingTests(unittest.TestCase):
    def test_saved_work(self):
        with self.assertRaises(SavedWorkError) as raised:
            SavedObjectService(None)._call(refuse)
        self.assertEqual((raised.exception.status_code,raised.exception.code),(413,'saved_work_limit_reached'))
        self.assertIn('portable backup capacity',raised.exception.message)

    def test_video_type_reviews(self):
        with self.assertRaises(ReviewError) as raised:
            VideoTypeReviewService._call(refuse)
        self.assertEqual((raised.exception.status,raised.exception.code),(413,'video_type_review_limit_reached'))

    def test_user_work_restore(self):
        with self.assertRaises(UserWorkError) as raised:
            UserWorkService(None).call(refuse)
        self.assertEqual((raised.exception.status,raised.exception.code),(413,'user_work_limit_exceeded'))

    def test_other_failures_stay_unavailable(self):
        with self.assertRaises(SavedWorkError) as raised:
            SavedObjectService(None)._call(lambda: (_ for _ in ()).throw(RuntimeError('user_work_limit_reached text')))
        self.assertEqual(raised.exception.status_code,503)

class OneLimitAnswerTests(unittest.TestCase):
    """An item count over a backup limit answered 422, a size 413; both are 413 now."""

    def test_an_item_count_over_the_limit_answers_like_the_size(self):
        from user_work_backup import LIMITS, MAX_BYTES, decode_backup, normalize_backup
        stamp='2026-09-01T00:00:00+00:00'
        def document(**work):
            return {'format':'leti-cook-user-work','version':1,'created_at':stamp,
                    'user_work':{'folders':[],'labels':[],'channels':[],'collections':[],'reviews':[],**work},
                    'browser_preferences':{'catalogue_density':'large','bookmarked_channel_refs':[],'collapsed_folder_ids':[]}}
        folders=[{'id':'a0000000-0000-4000-8000-%012d'%i,'name':'F','created_at':stamp} for i in range(LIMITS['folders']+1)]
        members=[{'video_id':'AAAAAAAAAA1','added_at':stamp}]*(LIMITS['members']+1)
        collection={'id':'b0000000-0000-4000-8000-000000000001','name':'C','revision':1,'created_at':stamp,
                    'updated_at':stamp,'members':members}
        calls=(lambda:normalize_backup(document(folders=folders)),          # one list over its limit
               lambda:normalize_backup(document(collections=[collection])),  # a total, checked before any item
               lambda:decode_backup(b' '*(MAX_BYTES+1)))                     # the file size
        answers=[]
        for call in calls:
            with self.assertRaises(UserWorkError) as raised:
                call()
            answers.append((raised.exception.code,raised.exception.status))
        self.assertEqual(answers,[('user_work_limit_exceeded',413)]*3)
        self.assertEqual(UserWorkError().status,422)
        self.assertEqual(UserWorkError('invalid_user_work_request').status,422)

class FolderRouteTests(unittest.TestCase):
    def create_folder(self, create):
        class Repository:
            def __init__(self, db): pass
            def create_folder(self, values): create(values)
        router = create_organization_router(cache=cache_stub(), library=library_stub(), database=None)
        route = endpoint(router, 'create_folder')
        def call(name):
            with patch('web.organization.ChannelOrganizationRepository', Repository):
                return route(FolderRequest(name=name))
        return call

    def test_a_full_workspace_refuses_a_folder_clearly(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as raised:
            self.create_folder(refuse)('Music')
        self.assertEqual(raised.exception.status_code,413)
        self.assertIn('1,000 folders',raised.exception.detail)
        with self.assertRaises(RuntimeError):
            self.create_folder(lambda values:(_ for _ in ()).throw(RuntimeError('other')))('Music')
        self.assertEqual(self.create_folder(lambda values:None)('Music')['name'],'Music')

class SchemaTests(unittest.TestCase):
    def test_every_backup_table_has_the_limit_trigger(self):
        for table in ('channel_label_assignments','channel_labels','channels','folders',
                      'saved_collection_members','saved_objects','video_type_review_history'):
            self.assertIn(f'CREATE TRIGGER user_work_backup_limits AFTER INSERT ON public.{table} FOR EACH STATEMENT '
                          'EXECUTE FUNCTION public.enforce_user_work_backup_limits();',SCHEMA)
        body=SCHEMA[SCHEMA.index('CREATE FUNCTION public.enforce_user_work_backup_limits()'):]
        body=body[:body.index('\n$$;')] if '\n$$;' in body[:4000] else body[:body.index('END $$;')]
        # The same numbers as export_user_work_backup.
        for bound in ('>10000','>1000','LIMIT 1002)x)>1001','>100000','LIMIT 10001)x)>10000'):
            self.assertIn(bound,body)
        self.assertIn("RAISE EXCEPTION 'user_work_limit_reached'",body)
        self.assertIn('REVOKE ALL ON FUNCTION public.enforce_user_work_backup_limits() FROM PUBLIC;',SCHEMA)

if __name__=='__main__':unittest.main()
