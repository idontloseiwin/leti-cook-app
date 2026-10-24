"""A repeated folder or label name reuses the item and says so; enabled is JSON true/false only (T-07). No database."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))

from http_test_support import cache_stub, library_stub, endpoint
from unittest.mock import patch
from web.organization import create_organization_router
from web.models import LabelAssignRequest


class Request:
    def __init__(self, name):
        self.name = name


class CreateFeedbackTests(unittest.TestCase):
    def test_a_folder_name_that_exists_is_reused_and_flagged(self):
        created = []

        class Repository:
            def __init__(self, db): pass
            def create_folder(self, values): created.append(values)
        router = create_organization_router(cache=cache_stub(), database=None,
            library=library_stub(load_folders=lambda: [{'id': 'f1', 'name': 'Music'}]))
        create = endpoint(router, 'create_folder')
        mock = patch('web.organization.ChannelOrganizationRepository', Repository)
        mock.start(); self.addCleanup(mock.stop)
        self.assertEqual(create(Request('music')), {'id': 'f1', 'name': 'Music', 'created': False})
        self.assertEqual(created, [])
        made = create(Request('Podcasts'))
        self.assertEqual((made['name'], made['created']), ('Podcasts', True))
        self.assertEqual([row['name'] for row in created], ['Podcasts'])

    def test_a_label_name_that_exists_is_reused_and_flagged(self):
        labels = [{'label_key': 'monetized', 'name': 'Monetized', 'label_type': 'preset'}]

        class Repository:
            def __init__(self, db): pass
            def load_labels(self): return list(labels)
            def create_label(self, row): labels.append(row); return dict(row)
        router = create_organization_router(cache=cache_stub(), library=library_stub(), database=None)
        create = endpoint(router, 'create_channel_label')
        mock = patch('web.organization.ChannelLabelRepository', Repository)
        mock.start(); self.addCleanup(mock.stop)
        self.assertEqual(create(Request(' monetized '))['created'], False)
        made = create(Request('Favourites'))
        self.assertEqual((made['name'], made['label_type'], made['created']), ('Favourites', 'custom', True))
        self.assertEqual(len(labels), 2)

    def test_enabled_accepts_only_json_booleans(self):
        from pydantic import ValidationError
        model = LabelAssignRequest
        self.assertIs(model.model_validate({'enabled': True}).enabled, True)
        self.assertIs(model.model_validate({'enabled': False}).enabled, False)
        for value in ('yes', 'true', 1, 0, None):
            with self.assertRaises(ValidationError, msg=repr(value)):
                model.model_validate({'enabled': value})


if __name__ == '__main__':
    unittest.main()
