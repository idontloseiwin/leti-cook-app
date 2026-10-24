"""Optional trash names cannot turn user-work access into a source read."""
from datetime import timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from channel_trash import create_channel_trash_router
from db.channel.channel_trash import ChannelTrashRepository
from hot_storage import capture_read_lifetime, iso_utc, utc_now, HOT_DATA_WINDOW
from hot_runtime_routes import route_access
from web.middleware import install_runtime_middleware

IDENTITY = '44444444-4444-4444-8444-444444444444'
OTHER = '55555555-5555-4555-8555-555555555555'


class TrashTitleTests(unittest.TestCase):
    def setUp(self):
        self.now = utc_now()
        self.database = Mock()
        self.repository = ChannelTrashRepository(self.database, read_clock=lambda: self.now)
        self.row = dict(user_work_id=IDENTITY, title='Example channel', channel_metadata_status='ok',
                        channel_metadata_checked_at=iso_utc(self.now-timedelta(days=1)),
                        source_expires_at=iso_utc(self.now+timedelta(days=28)))

    def client(self, repository=None):
        app = FastAPI()
        app.include_router(create_channel_trash_router(repository or self.repository, clock=lambda: self.now))
        with patch('web.middleware.selected_deployment_profile', return_value='operator'):
            install_runtime_middleware(app, core=SimpleNamespace())
        return TestClient(app)

    def test_source_query_is_bounded_hidden_and_reader_only(self):
        self.database.read_rows.return_value = [self.row]
        with capture_read_lifetime() as lifetime:
            rows = self.repository.removed_titles([IDENTITY])
        call = self.database.read_rows.call_args
        self.assertIn('FROM readable.channels_with_control WHERE is_hidden IS TRUE', call.args[1])
        self.assertIn('user_work_id=ANY(%s::uuid[])', call.args[1])
        self.assertEqual(call.args[2], ([IDENTITY],))
        self.assertEqual(call.kwargs['max_rows'], 100)
        self.assertEqual(rows[0]['title'], 'Example channel')
        self.assertIsNotNone(lifetime.expires_at)
        self.database.write.assert_not_called()

    def test_profile_failures_future_and_old_clocks_do_not_supply_name(self):
        for updates in ({'channel_metadata_status': 'error'},
                        {'channel_metadata_checked_at': iso_utc(self.now+timedelta(seconds=1))},
                        {'channel_metadata_checked_at': iso_utc(self.now-HOT_DATA_WINDOW-timedelta(seconds=1))}):
            with self.subTest(updates=updates):
                self.database.read_rows.return_value = [dict(self.row, **updates)]
                self.assertIsNone(self.repository.removed_titles([IDENTITY])[0]['title'])

    def test_title_response_preserves_item_and_envelope_deadline(self):
        self.database.read_rows.return_value = [self.row]
        with self.client() as client:
            response = client.get('/user-work/deleted-channels/titles', params={'ids': IDENTITY})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.json()['items'], [dict(id=IDENTITY, title='Example channel',
                                                       source_expires_at=self.row['source_expires_at'])])
        self.assertEqual(response.headers['x-source-expires-at'], self.row['source_expires_at'])

    def test_ordinary_trash_list_never_requests_titles(self):
        repository = Mock()
        repository.list_removed.return_value = [dict(user_work_id=IDENTITY, user_input='@example', hidden_at=iso_utc(self.now))]
        repository.removed_titles.side_effect = AssertionError('API data locked')
        with self.client(repository) as client:
            response = client.get('/user-work/deleted-channels')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['items'][0]['can_restore'])
        self.assertNotIn('title', response.json()['items'][0])
        repository.removed_titles.assert_not_called()
        self.assertEqual(route_access('GET', '/user-work/deleted-channels'), 'user_work')
        self.assertEqual(route_access('GET', '/user-work/deleted-channels/titles'), 'api_read')

    def test_bad_batches_are_rejected_before_any_read(self):
        with self.client() as client:
            for value in ('', 'invalid', IDENTITY+','+IDENTITY, ','.join([IDENTITY]*101)):
                self.assertEqual(client.get('/user-work/deleted-channels/titles', params={'ids': value}).status_code, 422)
        self.database.read_rows.assert_not_called()

    def test_expired_during_read_or_unrequested_identity_is_not_exposed(self):
        self.database.read_rows.return_value = [dict(self.row, source_expires_at=iso_utc(self.now))]
        with self.client() as client:
            self.assertEqual(client.get('/user-work/deleted-channels/titles', params={'ids': IDENTITY}).status_code, 503)
        repository = Mock()
        repository.removed_titles.return_value = [dict(self.row, user_work_id=OTHER)]
        with self.client(repository) as client:
            response = client.get('/user-work/deleted-channels/titles', params={'ids': IDENTITY})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('Example channel', response.text)


if __name__ == '__main__':
    unittest.main()
