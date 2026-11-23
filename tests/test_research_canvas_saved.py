"""A saved canvas (conditions version 4): only the graph, no channel binding, kept drafts, portable backups."""
from copy import deepcopy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
from research_query.graph import normalize_saved_canvas, saved_scopes
from saved_views import SavedViewsService
from saved_work import SavedWorkError, normalize_conditions
from test_backup_label_names import document
from test_research_channels import channel_query
from test_research_graph_v2 import GALLERY, VIEWS, canvas, source_params
from test_research_query_contract import query
from user_work_backup import decode_backup, encode_backup, import_payload, normalize_backup

FOLDER = 'a0000000-0000-4000-8000-000000000001'
CHANNEL = 'UC' + 'A' * 22
VIEW_ID = 'c0000000-0000-4000-8000-000000000041'


def saved_canvas():
    """Two sources (a folder's videos and one channel's videos), a branch each, and an unconnected draft."""
    folder = source_params(query(scope={'kind': 'folder', 'id': FOLDER, 'include_descendants': False}))
    channel = source_params(query(scope={'kind': 'channel', 'id': CHANNEL}))
    graph = canvas([('videos', 'source', folder), ('views', 'filter', {'where': VIEWS}), ('cards', 'gallery', GALLERY),
                    ('videos_2', 'source', channel), ('cards_2', 'gallery', GALLERY), ('draft', 'sort', {'order_by': [{'field': 'video.views', 'direction': 'desc'}]})],
                   [('videos', 'views'), ('views', 'cards'), ('videos_2', 'cards_2')])['graph']
    graph['notes'] = 'Which long uploads grew fastest?'
    return {'version': 4, 'graph': graph}


class Repository:
    """The few repository calls the service makes; rows are what the database would return."""
    def __init__(self, channels=(), scopes=()):
        self.channels, self.scopes, self.created, self.reads = set(channels), set(scopes), [], []

    def create_object(self, identity, name, digest, source_id, conditions):
        self.created.append((identity, source_id, conditions))
        return {'outcome': 'ready', 'object': self.get_object(identity)}

    def get_object(self, identity):
        _, _, conditions = self.created[-1]
        return {'kind': 'view', 'id': identity, 'name': 'My canvas', 'channel_id': None, 'conditions': conditions, 'revision': 1,
                'created_at': '2026-11-01T00:00:00+00:00', 'updated_at': '2026-11-01T00:00:00+00:00', 'deleted_at': None}

    def load_available_channels(self, ids):
        self.reads.append(('channels', list(ids)))
        return {i for i in ids if i in self.channels}

    def load_available_scopes(self, scopes):
        self.reads.append(('scopes', list(scopes)))
        return {s['kind'] + ':' + s['id'] for s in scopes if s['kind'] + ':' + s['id'] in self.scopes}


class SavedCanvasTests(unittest.TestCase):
    def test_roundtrip_keeps_sources_branches_drafts_notes_and_layout(self):
        value = saved_canvas()
        self.assertEqual(normalize_conditions(value), value)
        self.assertEqual(normalize_saved_canvas(deepcopy(value)), value)
        self.assertEqual(saved_scopes(value), [{'kind': 'folder', 'id': FOLDER, 'include_descendants': False}, {'kind': 'channel', 'id': CHANNEL}])

    def test_channel_sources_of_another_kind_open_too(self):
        value = saved_canvas()
        value['graph']['nodes'][3]['parameters'] = source_params(channel_query())
        self.assertEqual(normalize_conditions(value), value)

    def test_results_queries_and_unknown_keys_are_refused(self):
        for change in (lambda v: v.update(query=query()), lambda v: v.update(version=5), lambda v: v.pop('graph'),
                       lambda v: v['graph'].update(results={}), lambda v: v['graph']['nodes'][2]['parameters'].update(rows=[]),
                       lambda v: v['graph'].update(version=1)):
            value = saved_canvas(); change(value)
            with self.assertRaises(SavedWorkError):
                normalize_conditions(value)

    def test_a_canvas_is_bound_to_no_channel(self):
        repository = Repository()
        service = SavedViewsService(repository)
        with self.assertRaises(SavedWorkError):
            service.create(dict(id=VIEW_ID, name='My canvas', channel_id=CHANNEL, conditions=saved_canvas()))
        self.assertEqual(repository.created, [])
        created = service.create(dict(id=VIEW_ID, name='My canvas', channel_id=None, conditions=saved_canvas()))['view']
        self.assertEqual(repository.created[0][1], None)
        self.assertEqual(created['conditions'], saved_canvas())

    def test_available_only_when_every_source_is(self):
        for channels, scopes, expected in (((), (), False), ((CHANNEL,), (), False), ((), ('folder:' + FOLDER,), False),
                                           ((CHANNEL,), ('folder:' + FOLDER,), True)):
            repository = Repository(channels, scopes)
            service = SavedViewsService(repository)
            view = service.create(dict(id=VIEW_ID, name='My canvas', channel_id=None, conditions=saved_canvas()))['view']
            self.assertEqual(view['channel_available'], expected)
            self.assertIn(('channels', [CHANNEL]), repository.reads)

    def test_save_and_backup_agree_on_8192_byte_notes_and_name_a_size_refusal(self):
        """4,096 Cyrillic letters save, restore and round-trip; one more is refused in words, never as invalid."""
        long_notes = saved_canvas()
        long_notes['graph']['notes'] = 'ж' * 4096
        self.assertEqual(normalize_saved_canvas(long_notes), long_notes)
        self.assertEqual(normalize_conditions(long_notes), long_notes)
        backup = normalize_backup(document())
        view = dict(id=VIEW_ID, name='My canvas', revision=1, created_at='2026-11-01T00:00:00Z', updated_at='2026-11-01T00:00:00Z', conditions=long_notes)
        backup['user_work']['queries'] = [view]
        self.assertEqual(import_payload(normalize_backup(backup))['queries'][0]['conditions'], long_notes)
        too_long = deepcopy(long_notes)
        too_long['graph']['notes'] += 'ж'
        with self.assertRaises(SavedWorkError) as refused:
            normalize_conditions(too_long)
        self.assertEqual(refused.exception.message, 'Notes are limited to 8,192 bytes (about 4,096 Cyrillic letters).')
        backup['user_work']['queries'] = [dict(view, conditions=too_long)]
        with self.assertRaises(Exception):
            normalize_backup(backup)
        crowded = saved_canvas()
        crowded['graph']['nodes'].append({'id': 'picked', 'type': 'selection', 'parameters': {'mode': 'include', 'ids': [f'v{i:010d}' for i in range(1000)]}})
        crowded['graph']['layout']['picked'] = {'x': 0, 'y': 500}
        crowded['graph']['notes'] = 'n' * 3000
        with self.assertRaises(SavedWorkError) as whole:
            normalize_conditions(crowded)
        self.assertEqual(whole.exception.message, 'A canvas is limited to 16,384 bytes.')
        # A saved canvas from before (short notes) stays readable.
        self.assertEqual(normalize_conditions(saved_canvas()), saved_canvas())

    def test_backup_keeps_a_canvas_with_the_unbound_queries_only(self):
        backup = normalize_backup(document())
        view = dict(id=VIEW_ID, name='My canvas', revision=1, created_at='2026-11-01T00:00:00Z', updated_at='2026-11-01T00:00:00Z', conditions=saved_canvas())
        backup['user_work']['queries'] = [view]
        normalized = normalize_backup(backup)
        self.assertEqual(normalized['user_work']['queries'][0]['conditions'], saved_canvas())
        self.assertEqual(decode_backup(encode_backup(normalized)), normalized)
        self.assertEqual(import_payload(normalized)['queries'][0]['conditions'], saved_canvas())
        bound = normalize_backup(document())
        bound['user_work']['channels'] = [dict(id='c0000000-0000-4000-8000-000000000042', user_input=None, folder_id=None, parent_ref=None,
                                              is_hidden=False, added_at='2026-11-01T00:00:00+00:00', label_keys=[], saved_views=[view])]
        with self.assertRaises(Exception):
            normalize_backup(bound)


if __name__ == '__main__':
    unittest.main()
