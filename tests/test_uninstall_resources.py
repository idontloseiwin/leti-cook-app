"""No Docker calls: adversarial ownership, shared references and resumable deletion."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops import uninstall_resources as resources

PROJECT = 'leti-cook-0123456789ab'
INSTALLATION = '/opt/leti-cook-fixture'
OP = 'a' * 32
DB, BACKEND, FOREIGN, HELPER = ('1' * 64, '2' * 64, '3' * 64, '4' * 64)
DB_IMAGE, APP_IMAGE = 'sha256:' + 'd' * 64, 'sha256:' + 'b' * 64
DB_NET, APP_NET = '5' * 64, '6' * 64


def compose_container(kind, ident, image):
    return {'id': ident, 'name': '/' + PROJECT + '-' + kind + '-1', 'image': image, 'running': True,
        'labels': {resources.PROJECT: PROJECT, resources.SERVICE: kind, resources.ONEOFF: 'False', resources.WORKDIR: INSTALLATION + '/database'},
        'volumes': [PROJECT + ('_database-data' if kind == 'db' else '_application-state')]}


def volume(name, logical='database-data', operation=None):
    labels = {resources.PROJECT: PROJECT, resources.VOLUME: logical}
    if operation:
        labels[resources.OPERATION] = operation
    return {'name': name, 'created_at': '2026-10-09T12:00:00Z', 'driver': 'local', 'options': {}, 'labels': labels}


class DockerFixture:
    def __init__(self):
        self.calls = []
        self.raw_calls = []
        self.context_endpoint = 'unix:///var/run/docker.sock'
        self.ostype = 'linux'
        self.containers = {DB: compose_container('db', DB, DB_IMAGE), BACKEND: compose_container('backend', BACKEND, APP_IMAGE)}
        self.volumes = {PROJECT + '_database-data': volume(PROJECT + '_database-data'),
                        PROJECT + '_application-state': volume(PROJECT + '_application-state', 'application-state')}
        self.networks = {ident: {'id': ident, 'name': PROJECT + '_' + logical, 'driver': 'bridge',
            'internal': logical == 'database', 'labels': {resources.PROJECT: PROJECT, resources.NETWORK: logical},
            'endpoints': [DB, BACKEND] if logical == 'database' else [BACKEND]}
            for ident, logical in ((DB_NET, 'database'), (APP_NET, 'application-access'))}
        self.images = {ident: {'id': ident, 'tags': [PROJECT + '-' + kind + ':latest'], 'repo_digests': [], 'labels': {}}
                       for ident, kind in ((DB_IMAGE, 'db'), (APP_IMAGE, 'backend'))}

    def foreign(self, *, image=APP_IMAGE, volumes=()):
        self.containers[FOREIGN] = {'id': FOREIGN, 'name': '/other-fixture', 'image': image, 'running': False,
                                    'labels': {}, 'volumes': list(volumes)}

    @property
    def mutations(self):
        return [call for call in self.calls if call[1] in ('update', 'stop') or len(call) > 2 and call[2] == 'rm']

    def run(self, args, *, timeout):
        assert args[0] == 'docker' and 0 < timeout <= 30
        self.raw_calls.append(args[:])
        if args[1:2] == ['--host']:
            assert args[2].startswith('unix:///')
            args = [args[0], *args[3:]]
        self.calls.append(args[:])
        kind, action = args[1:3]
        if kind == 'context':
            assert action == 'inspect'
            return self.context_endpoint
        if kind == 'info':
            assert args[2:] == ['--format', '{{json .OSType}}']
            return json.dumps(self.ostype)
        tables = {'container': self.containers, 'volume': self.volumes, 'network': self.networks, 'image': self.images}
        if action == 'ls':
            table = tables[kind]
            values = list(table.values())
            for index, arg in enumerate(args):
                if arg != '--filter':
                    continue
                filter_value = args[index + 1]
                if filter_value.startswith('label='):
                    key, value = filter_value[6:].split('=', 1)
                    values = [item for item in values if item['labels'].get(key) == value]
                elif filter_value.startswith('reference='):
                    value = filter_value[10:]
                    values = [item for item in values if value in item['tags']]
                else:
                    raise AssertionError('unrecognized filter')
            if '--format' in args:
                assert kind == 'network' and args[args.index('--format') + 1] == '{{.ID}} {{.Name}}'
                return '\n'.join(item['id'] + ' ' + item['name'] for item in values)
            return '\n'.join(item['name' if kind == 'volume' else 'id'] for item in values)
        if action == 'inspect':
            template = args[args.index('--format') + 1]
            identities = args[args.index('--format') + 2:]
            output = []
            for ident in identities:
                original = tables[kind][ident]
                if kind == 'container' and template == resources._GLOBAL_FORMAT:
                    value = {key: copy.deepcopy(original[key]) for key in ('id', 'name', 'image', 'volumes')}
                    value['volumes'] = [None, *value['volumes']]
                else:
                    value = copy.deepcopy(original)
                    value.pop('volumes', None)
                    if kind == 'network':
                        value['endpoints'] = [None, *value['endpoints']]
                output.append(json.dumps(value))
            return '\n'.join(output)
        if kind == 'update':
            assert action == '--restart=no'
            return args[-1]
        if kind == 'stop':
            assert args[2:4] == ['--time', '10']
            self.containers[args[-1]]['running'] = False
            return args[-1]
        if action == 'rm':
            ident = args[-1]
            if kind == 'container':
                assert not self.containers[ident]['running'] and '-f' not in args
                del self.containers[ident]
                for item in self.networks.values():
                    if ident in item['endpoints']:
                        item['endpoints'].remove(ident)
            elif kind == 'volume':
                assert not any(ident in item['volumes'] for item in self.containers.values())
                del self.volumes[ident]
            elif kind == 'network':
                assert not self.networks[ident]['endpoints']
                del self.networks[ident]
            else:
                assert '--no-prune' in args and '-f' not in args
                image = next(item for item in self.images.values() if ident == item['id'] or ident in item['tags'])
                assert not any(item['image'] == image['id'] for item in self.containers.values())
                if ident in image['tags']:
                    image['tags'].remove(ident)
                if not image['tags']:
                    del self.images[image['id']]
            return ident
        raise AssertionError('unrecognized command')


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.platform = patch.object(resources.sys, 'platform', 'linux')
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.environment = patch.dict(os.environ, {'DOCKER_HOST': '', 'DOCKER_CONTEXT': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.docker = DockerFixture()
        self.resources = resources.DockerResources(PROJECT, INSTALLATION, self.docker.run)

    def inspect(self, journal=None):
        return self.resources.inspect_plan(journal)

    def remove_services(self, plan):
        self.resources.stop(plan)
        self.resources.remove_containers(plan)

    def journal(self):
        return {'project': PROJECT, 'operation_id': OP, 'old_images': {'db': DB_IMAGE, 'backend': APP_IMAGE},
            'new_images': None, 'old_volume': PROJECT + '_database-data', 'restore_volume': None}

    def test_inventory_detached_exact_owned_metadata_no_mutations(self):
        plan = self.inspect()
        self.assertEqual([len(plan[key]) for key in ('containers', 'volumes', 'networks', 'images')], [2, 2, 2, 2])
        self.assertEqual(self.docker.mutations, [])
        checked = resources.validate_inventory(plan)
        plan['containers'][0]['labels'][resources.PROJECT] = 'changed'
        self.assertEqual(checked['containers'][0]['labels'][resources.PROJECT], PROJECT)
        for command in self.docker.calls:
            self.assertNotIn('{{json .}}', command)
            self.assertFalse(any('.Env' in part for part in command))
        raw = json.dumps(checked)
        for forbidden in ('Mountpoint', 'Destination', 'Source', 'Password', 'Env', 'RestartPolicy'):
            self.assertNotIn(forbidden, raw)

    def test_complete_cleanup_order_no_force_or_prune_and_idempotent_resume(self):
        plan = self.inspect()
        stopped = self.resources.stop(plan)
        self.assertEqual(stopped['stopped'], [BACKEND, DB])
        for ident in stopped['stopped']:
            update = self.docker.calls.index(['docker', 'update', '--restart=no', ident])
            stop = self.docker.calls.index(['docker', 'stop', '--time', '10', ident])
            self.assertLess(update, stop)
        self.assertEqual(len(self.resources.remove_containers(plan)['removed']), 2)
        self.assertEqual(len(self.resources.remove_volumes(plan)['removed']), 2)
        self.assertEqual(len(self.resources.remove_networks(plan)['removed']), 2)
        self.assertEqual(len(self.resources.remove_images(plan)['removed']), 2)
        self.assertEqual(self.resources.verify_removed(plan), {'complete': True})
        for method in (self.resources.stop, self.resources.remove_containers, self.resources.remove_volumes,
                       self.resources.remove_networks, self.resources.remove_images):
            result = method(plan)
            self.assertEqual(len(result['absent']), 2)
        for command in self.docker.mutations:
            self.assertNotIn('-f', command)
            self.assertNotIn('--force', command)
            self.assertNotIn('prune', command)

    def test_inherited_image_owner_allows_fresh_container_removal(self):
        for item in self.docker.containers.values():
            item['labels'][resources.OWN_PROJECT] = PROJECT
        plan = self.inspect()
        self.assertEqual(self.docker.mutations, [])
        self.assertEqual([item['labels'][resources.OWN_PROJECT] for item in plan['containers']], [PROJECT, PROJECT])
        self.remove_services(plan)
        self.assertEqual(self.docker.containers, {})

    def test_inherited_historical_operation_is_not_the_current_helper_operation(self):
        for item in self.docker.containers.values():
            item['labels'].update({resources.OWN_PROJECT: PROJECT, resources.OPERATION: 'b' * 32})
        for journal in (None, self.journal()):
            with self.subTest(journal=journal is not None):
                plan = self.inspect(journal)
                self.assertEqual(plan['operation_id'], OP if journal else None)
                self.assertEqual([item['labels'][resources.OPERATION] for item in plan['containers']], ['b' * 32] * 2)
        self.assertEqual(self.docker.mutations, [])
        self.remove_services(plan)
        self.assertEqual(self.docker.containers, {})

    def test_inherited_labels_cannot_replace_compose_identity_or_authorize_foreign_owner(self):
        original = copy.deepcopy(self.docker.containers[DB]['labels'])
        captured = self.inspect()
        valid = {**original, resources.OWN_PROJECT: PROJECT, resources.OPERATION: 'b' * 32}
        cases = [('foreign owner', {**valid, resources.OWN_PROJECT: 'leti-cook-ffffffffffff'}),
                 ('operation without owner', {**original, resources.OPERATION: 'b' * 32}),
                 ('foreign working directory', {**valid, resources.WORKDIR: '/opt/other'})]
        for operation in ('b' * 31, 'b' * 33, 'B' * 32, 'g' * 32):
            cases.append(('bad operation ' + operation, {**valid, resources.OPERATION: operation}))
        for key in (resources.PROJECT, resources.SERVICE, resources.ONEOFF, resources.WORKDIR):
            cases.append(('missing ' + key, {name: value for name, value in valid.items() if name != key}))
        for name, labels in cases:
            with self.subTest(case=name):
                invalid = copy.deepcopy(captured)
                next(item for item in invalid['containers'] if item['id'] == DB)['labels'] = labels
                with self.assertRaisesRegex(resources.ResourceError, 'uninstall_container_not_owned'):
                    resources.validate_inventory(invalid)
                self.docker.containers[DB]['labels'] = labels
                with self.assertRaises(resources.ResourceError):
                    self.inspect()
                self.assertEqual(self.docker.mutations, [])
        self.docker.containers[DB]['labels'] = original

    def test_inherited_operation_cannot_change_after_inventory(self):
        self.docker.containers[BACKEND]['labels'].update({resources.OWN_PROJECT: PROJECT, resources.OPERATION: 'b' * 32})
        plan = self.inspect()
        self.docker.containers[BACKEND]['labels'][resources.OPERATION] = 'c' * 32
        with self.assertRaisesRegex(resources.ResourceError, 'uninstall_container_identity_changed'):
            self.resources.stop(plan)
        self.assertEqual(self.docker.mutations, [])

    def test_foreign_labels_name_working_dir_and_unknown_service_refused(self):
        for mutate in (lambda row: row['labels'].update({resources.PROJECT: 'leti-cook-ffffffffffff'}),
                lambda row: row['labels'].update({resources.WORKDIR: '/other'}),
                lambda row: row['labels'].update({resources.SERVICE: 'unknown'}),
                lambda row: row['labels'].update({resources.ONEOFF: 'True'}),
                lambda row: row.update(name='/some-other-container')):
            before = copy.deepcopy(self.docker.containers[DB])
            mutate(self.docker.containers[DB])
            with self.assertRaises(resources.ResourceError):
                self.inspect()
            self.assertEqual(self.docker.mutations, [])
            self.docker.containers[DB] = before

    def test_exact_journal_helper_captured_and_stopped_first(self):
        self.docker.containers[HELPER] = {'id': HELPER, 'name': '/' + PROJECT + '-update-' + OP + '-helper',
            'image': APP_IMAGE, 'running': True, 'volumes': [],
            'labels': {resources.OWN_PROJECT: PROJECT, resources.OPERATION: OP}}
        with self.assertRaises(resources.ResourceError):
            self.inspect()
        plan = self.inspect(self.journal())
        self.assertEqual(self.resources.stop(plan)['stopped'], [HELPER, BACKEND, DB])
        self.docker.containers[HELPER]['labels'][resources.OPERATION] = 'b' * 32
        with self.assertRaisesRegex(resources.ResourceError, 'identity_changed'):
            self.resources.remove_containers(plan)

    def test_unknown_helper_image_is_not_accepted(self):
        self.docker.containers[HELPER] = {'id': HELPER, 'name': '/' + PROJECT + '-update-' + OP + '-helper',
            'image': 'sha256:' + 'e' * 64, 'running': True, 'volumes': [],
            'labels': {resources.OWN_PROJECT: PROJECT, resources.OPERATION: OP}}
        with self.assertRaisesRegex(resources.ResourceError, 'helper_image_not_recorded'):
            self.inspect(self.journal())

    def test_foreign_volume_reference_even_stopped_refuses_inventory(self):
        self.docker.foreign(volumes=[PROJECT + '_database-data'])
        with self.assertRaisesRegex(resources.ResourceError, 'foreign_reference'):
            self.inspect()
        self.assertEqual(self.docker.mutations, [])

    def test_restored_volume_requires_journal_or_current_db_mount_and_exact_op(self):
        name = PROJECT + '_restore_' + OP
        self.docker.volumes[name] = volume(name, operation=OP)
        with self.assertRaisesRegex(resources.ResourceError, 'unrecorded_project_volume'):
            self.inspect()
        self.assertIn(name, [item['name'] for item in self.inspect(self.journal())['volumes']])
        self.docker.containers[DB]['volumes'] = [name]
        self.assertIn(name, [item['name'] for item in self.inspect()['volumes']])
        self.docker.volumes[name]['labels'][resources.OPERATION] = 'b' * 32
        with self.assertRaisesRegex(resources.ResourceError, 'volume_not_owned'):
            self.inspect()

    def test_plain_volume_driver_and_creation_identity_checked(self):
        for key, value in (('driver', 'nfs'), ('options', {'device': '/private/foreign'}),
                           ('labels', {resources.PROJECT: PROJECT, resources.VOLUME: 'other'})):
            before = copy.deepcopy(self.docker.volumes[PROJECT + '_database-data'])
            self.docker.volumes[PROJECT + '_database-data'][key] = value
            with self.assertRaises(resources.ResourceError):
                self.inspect()
            self.docker.volumes[PROJECT + '_database-data'] = before
        plan = self.inspect()
        self.remove_services(plan)
        self.docker.volumes[PROJECT + '_database-data']['created_at'] = '2026-10-09T13:00:00Z'
        with self.assertRaisesRegex(resources.ResourceError, 'volume_identity_changed'):
            self.resources.remove_volumes(plan)

    def test_late_foreign_reference_blocks_removal_without_touching_foreign_container(self):
        plan = self.inspect()
        self.remove_services(plan)
        self.docker.foreign(volumes=[PROJECT + '_database-data'])
        before = copy.deepcopy(self.docker.containers[FOREIGN])
        with self.assertRaisesRegex(resources.ResourceError, 'still_referenced'):
            self.resources.remove_volumes(plan)
        self.assertEqual(self.docker.containers[FOREIGN], before)
        self.assertIn(PROJECT + '_database-data', self.docker.volumes)

    def test_network_driver_internal_foreign_endpoint_and_same_name_collision(self):
        for mutate in (lambda item: item.update(driver='overlay'), lambda item: item.update(internal=False),
                lambda item: item['endpoints'].append(FOREIGN), lambda item: item['labels'].clear()):
            before = copy.deepcopy(self.docker.networks[DB_NET])
            mutate(self.docker.networks[DB_NET])
            with self.assertRaises(resources.ResourceError):
                self.inspect()
            self.docker.networks[DB_NET] = before
        self.assertEqual(self.docker.mutations, [])

    def test_late_network_endpoint_blocks_delete(self):
        plan = self.inspect()
        self.remove_services(plan)
        self.docker.networks[DB_NET]['endpoints'].append(FOREIGN)
        with self.assertRaisesRegex(resources.ResourceError, 'still_connected'):
            self.resources.remove_networks(plan)
        self.assertIn(DB_NET, self.docker.networks)

    def test_foreign_stopped_image_reference_retained_and_unchanged(self):
        self.docker.foreign()
        plan = self.inspect()
        before = copy.deepcopy(self.docker.containers[FOREIGN])
        self.remove_services(plan)
        result = self.resources.remove_images(plan)
        self.assertEqual(result['retained'], [{'id': APP_IMAGE, 'reason': 'container_reference'}])
        self.assertEqual(self.docker.containers[FOREIGN], before)
        self.assertIn(APP_IMAGE, self.docker.images)

    def test_foreign_tag_or_published_digest_retains_entire_shared_image(self):
        for field, value, reason in (('tags', ['another-app:latest'], 'foreign_tag'),
                                    ('repo_digests', ['ghcr.io/example/app@sha256:' + 'a' * 64], 'published_digest')):
            with self.subTest(field=field):
                self.setUp()
                self.docker.images[APP_IMAGE][field].extend(value)
                plan = self.inspect()
                self.remove_services(plan)
                result = self.resources.remove_images(plan)
                self.assertEqual(result['retained'], [{'id': APP_IMAGE, 'reason': reason}])
                self.assertIn(PROJECT + '-backend:latest', self.docker.images[APP_IMAGE]['tags'])

    def test_local_build_digests_allow_owned_fresh_and_update_image_removal(self):
        for ident, kind in ((DB_IMAGE, 'db'), (APP_IMAGE, 'backend')):
            image = self.docker.images[ident]
            image['labels'] = {resources.OWN_PROJECT: PROJECT, resources.OPERATION: OP}
            update_repository = PROJECT + '-update-' + OP + '-' + kind
            image['tags'].append(update_repository + ':latest')
            image['repo_digests'] = [PROJECT + '-' + kind + '@' + ident, update_repository + '@' + ident]
        plan = self.inspect()
        self.remove_services(plan)
        result = self.resources.remove_images(plan)
        self.assertCountEqual(result['removed'], [APP_IMAGE, DB_IMAGE])
        self.assertEqual(result['retained'], [])
        self.assertFalse(self.docker.images)
        calls = [call for call in self.docker.mutations if call[1:3] == ['image', 'rm']]
        self.assertTrue(calls)
        self.assertTrue(all('--no-prune' in call and '-f' not in call for call in calls))

    def test_own_digest_does_not_supply_missing_positive_ownership(self):
        image = self.docker.images[APP_IMAGE]
        image['repo_digests'] = [PROJECT + '-backend@' + APP_IMAGE]
        image['tags'] = []
        plan = self.inspect()
        self.remove_services(plan)
        result = self.resources.remove_images(plan)
        self.assertEqual(result['retained'], [{'id': APP_IMAGE, 'reason': 'ownership_unproven'}])

    def test_foreign_digest_kept_when_captured_or_added_after_confirmation(self):
        for captured in (False, True):
            with self.subTest(captured=captured):
                self.setUp()
                image = self.docker.images[APP_IMAGE]
                own = PROJECT + '-backend@' + APP_IMAGE
                foreign = 'ghcr.io/example/app@' + APP_IMAGE
                image['repo_digests'] = [own, foreign] if captured else [own]
                plan = self.inspect()
                self.remove_services(plan)
                image['repo_digests'] = [own] if captured else [own, foreign]
                result = self.resources.remove_images(plan)
                self.assertEqual(result['retained'], [{'id': APP_IMAGE, 'reason': 'published_digest'}])
                self.assertIn(APP_IMAGE, self.docker.images)

    def test_digest_repository_requires_exact_own_name_and_digest(self):
        invalid = [PROJECT + '-backend-extra@' + APP_IMAGE,
                   PROJECT + '-update-short-backend@' + APP_IMAGE,
                   PROJECT + '-backend:latest@' + APP_IMAGE,
                   'docker.io/library/' + PROJECT + '-backend@' + APP_IMAGE,
                   'another-' + PROJECT + '-backend@' + APP_IMAGE,
                   PROJECT + '-backend@sha256:abc']
        for reference in invalid:
            with self.subTest(reference=reference):
                self.setUp()
                self.docker.images[APP_IMAGE]['repo_digests'] = [reference]
                plan = self.inspect()
                self.remove_services(plan)
                result = self.resources.remove_images(plan)
                self.assertEqual(result['retained'], [{'id': APP_IMAGE, 'reason': 'published_digest'}])

    def test_own_digest_keeps_other_exclusive_ownership_checks(self):
        for condition, reason in (('container', 'container_reference'), ('tag', 'foreign_tag'),
                                  ('owner', 'foreign_owner')):
            with self.subTest(condition=condition):
                self.setUp()
                image = self.docker.images[APP_IMAGE]
                image['repo_digests'] = [PROJECT + '-backend@' + APP_IMAGE]
                if condition == 'container':
                    self.docker.foreign()
                elif condition == 'tag':
                    image['tags'].append('other-app:latest')
                else:
                    image['labels'] = {resources.OWN_PROJECT: 'another-project'}
                plan = self.inspect()
                self.remove_services(plan)
                result = self.resources.remove_images(plan)
                self.assertEqual(result['retained'], [{'id': APP_IMAGE, 'reason': reason}])

    def test_dangling_unlabelled_image_has_no_positive_ownership(self):
        self.docker.images[APP_IMAGE]['tags'] = []
        plan = self.inspect()
        self.remove_services(plan)
        self.assertEqual(self.resources.remove_images(plan)['retained'], [{'id': APP_IMAGE, 'reason': 'ownership_unproven'}])

    def test_foreign_image_project_label_is_retained_not_treated_as_owned(self):
        self.docker.images[APP_IMAGE]['labels'] = {resources.PROJECT: 'foreign-upstream-app'}
        plan = self.inspect()
        self.remove_services(plan)
        self.assertEqual(self.resources.remove_images(plan)['retained'], [{'id': APP_IMAGE, 'reason': 'foreign_owner'}])

    def test_owned_update_image_labels_and_multiple_own_tags_removed_without_force(self):
        self.docker.images[APP_IMAGE]['labels'] = {resources.OWN_PROJECT: PROJECT, resources.OPERATION: OP}
        self.docker.images[APP_IMAGE]['tags'].append(PROJECT + '-update-' + OP + '-backend:latest')
        plan = self.inspect()
        self.remove_services(plan)
        result = self.resources.remove_images(plan)
        self.assertIn(APP_IMAGE, result['removed'])
        self.assertFalse(self.docker.images)

    def test_image_shared_after_confirmation_retained(self):
        plan = self.inspect()
        self.remove_services(plan)
        self.docker.images[APP_IMAGE]['tags'].append('foreign:latest')
        result = self.resources.remove_images(plan)
        self.assertEqual(result['retained'], [{'id': APP_IMAGE, 'reason': 'foreign_tag'}])

    def test_running_container_cannot_be_removed_and_identity_is_rechecked(self):
        plan = self.inspect()
        with self.assertRaisesRegex(resources.ResourceError, 'still_running'):
            self.resources.remove_containers(plan)
        self.docker.containers[DB]['labels'][resources.WORKDIR] = '/foreign'
        with self.assertRaisesRegex(resources.ResourceError, 'identity_changed'):
            self.resources.stop(plan)
        self.assertIn(DB, self.docker.containers)

    def test_unknown_plan_fields_paths_and_ids_never_reach_docker(self):
        original = self.inspect()
        for mutate in (lambda v: v.update(extra=True), lambda v: v.update(version=True),
                lambda v: v.update(installation='/opt/../foreign'), lambda v: v.update(project='arbitrary'),
                lambda v: v['containers'][0].update(id='--force'), lambda v: v['volumes'][0].update(name='foreign'),
                lambda v: v['images'][0].update(tags=['--force'])):
            plan = copy.deepcopy(original)
            mutate(plan)
            calls = len(self.docker.calls)
            with self.assertRaises(resources.ResourceError):
                self.resources.stop(plan)
            self.assertEqual(len(self.docker.calls), calls)

    def test_docker_unavailable_is_not_absence_and_does_not_mutate(self):
        def unavailable(*args, **kwargs):
            raise RuntimeError('raw secret-bearing output must not escape')
        facade = resources.DockerResources(PROJECT, INSTALLATION, unavailable)
        with self.assertRaisesRegex(resources.ResourceError, '^uninstall_docker_command_failed$'):
            facade.inspect_plan()

    def test_constructor_and_pure_validator_do_not_require_daemon(self):
        def unavailable(*args, **kwargs):
            raise AssertionError('constructor must not touch Docker')
        resources.DockerResources(PROJECT, INSTALLATION, unavailable)
        plan = self.inspect()
        calls = len(self.docker.calls)
        resources.validate_inventory(plan)
        self.assertEqual(len(self.docker.calls), calls)

    def test_remote_host_and_remote_context_refused_before_daemon_contact(self):
        with patch.dict(os.environ, {'DOCKER_HOST': 'ssh://foreign-host', 'DOCKER_CONTEXT': ''}):
            with self.assertRaisesRegex(resources.ResourceError, 'local_docker_required'):
                self.inspect()
        self.assertEqual(self.docker.calls, [])
        self.docker.context_endpoint = 'tcp://192.0.2.1:2375'
        with patch.dict(os.environ, {'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_CONTEXT': 'remote-context'}):
            with self.assertRaisesRegex(resources.ResourceError, 'local_docker_required'):
                self.inspect()
        self.assertEqual(self.docker.calls, [['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}', 'remote-context']])

    def test_context_precedence_and_explicit_endpoint_persist_across_resume(self):
        with patch.dict(os.environ, {'DOCKER_HOST': 'ssh://ignored-host', 'DOCKER_CONTEXT': 'local-context'}):
            plan = self.inspect()
        self.assertEqual(plan['docker_host'], 'unix:///var/run/docker.sock')
        self.docker.context_endpoint = 'ssh://now-remote'
        resumed = resources.DockerResources(PROJECT, INSTALLATION, self.docker.run)
        begin = len(self.docker.raw_calls)
        with patch.dict(os.environ, {'DOCKER_HOST': 'tcp://192.0.2.1:2375', 'DOCKER_CONTEXT': 'changed-context'}):
            resumed.stop(plan)
        for command in self.docker.raw_calls[begin:]:
            self.assertEqual(command[:3], ['docker', '--host', 'unix:///var/run/docker.sock'])

    def test_linux_host_and_linux_daemon_required_even_with_injected_runner(self):
        with patch.object(resources.sys, 'platform', 'win32'):
            with self.assertRaisesRegex(resources.ResourceError, 'uninstall_linux_required'):
                self.inspect()
        self.assertEqual(self.docker.calls, [])
        self.docker.ostype = 'windows'
        with self.assertRaisesRegex(resources.ResourceError, 'linux_docker_required'):
            self.inspect()
        self.assertEqual(self.docker.mutations, [])

    def test_verification_detects_new_project_container_after_snapshot(self):
        plan = self.inspect()
        self.remove_services(plan)
        self.resources.remove_volumes(plan)
        self.resources.remove_networks(plan)
        self.docker.containers[DB] = compose_container('db', DB, DB_IMAGE)
        with self.assertRaisesRegex(resources.ResourceError, 'container_remaining'):
            self.resources.verify_removed(plan)


if __name__ == '__main__':
    unittest.main()
