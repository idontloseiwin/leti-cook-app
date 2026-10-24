"""Bounded Docker ownership observations and checked, resumable uninstall effects.

No Compose configuration, environment, SQL or filesystem contents are read.
The caller owns confirmation, the durable uninstall journal and the host lock.
Only exact captured resources are changed, and each effect rechecks ownership.
"""
from __future__ import annotations

import json
import os
from pathlib import PurePosixPath
import re
import sys

PROJECT = 'com.docker.compose.project'
SERVICE = 'com.docker.compose.service'
WORKDIR = 'com.docker.compose.project.working_dir'
ONEOFF = 'com.docker.compose.oneoff'
VOLUME = 'com.docker.compose.volume'
NETWORK = 'com.docker.compose.network'
OWN_PROJECT = 'org.leti-cook.project'
OPERATION = 'org.leti-cook.update-operation'
MAX_OUTPUT = 8 * 1024 * 1024
MAX_GLOBAL = 10000
_ID = re.compile(r'[0-9a-f]{64}')
_IMAGE = re.compile(r'sha256:[0-9a-f]{64}')
_OP = re.compile(r'[0-9a-f]{32}')
_PROJECT = re.compile(r'leti-cook-[0-9a-f]{12}')
_TAG = re.compile(r'[A-Za-z0-9][A-Za-z0-9_./:@-]{0,511}')


class ResourceError(ValueError):
    """Fixed codes only; never raw Docker output or private installation values."""


def _require(value, code='uninstall_inventory_invalid'):
    if not value:
        raise ResourceError(code)


def _exact(value, keys):
    _require(type(value) is dict and set(value) == set(keys))


def _string(value, maximum=512):
    _require(type(value) is str and 0 < len(value) <= maximum and all(32 <= ord(c) < 127 for c in value))
    return value


def _path(value):
    _string(value)
    path = PurePosixPath(value)
    _require(path.is_absolute() and str(path) == value and path != PurePosixPath('/')
             and re.fullmatch(r'/[A-Za-z0-9_./-]+', value) is not None
             and not any(part in ('.', '..') for part in path.parts), 'uninstall_installation_invalid')
    return value


def _project(value):
    _require(type(value) is str and _PROJECT.fullmatch(value) is not None, 'uninstall_project_invalid')
    return value


def _docker_host(value):
    _require(type(value) is str and value.startswith('unix://'), 'uninstall_local_docker_required')
    _path(value[7:])
    return value


def _identifier(value, *, image=False):
    _require(type(value) is str and (_IMAGE if image else _ID).fullmatch(value) is not None)
    return value


def _operation(value):
    _require(value is None or type(value) is str and _OP.fullmatch(value) is not None)
    return value


def _list(value, maximum):
    _require(type(value) is list and len(value) <= maximum)
    return value


def _unique(values):
    _require(len(values) == len(set(values)))


def _labels(value, keys):
    _require(type(value) is dict and set(value) <= set(keys))
    for key, item in value.items():
        _string(item)
    return value


def _volume_name(name, project):
    _require(type(name) is str and re.fullmatch(re.escape(project) + r'_(?:database-data|application-state|restore_[0-9a-f]{32})', name) is not None,
             'uninstall_volume_name_invalid')
    return name


def _own_tag(tag, project):
    return type(tag) is str and re.fullmatch(re.escape(project) + r'-(?:(?:db|backend)|update-[0-9a-f]{32}-(?:db|backend))(?::latest)?', tag) is not None


def _own_digest(reference, project):
    # A local build can have RepoDigests. Only the exact installation-owned
    # repository grammar is local; registry prefixes and lookalikes stay shared.
    return type(reference) is str and re.fullmatch(re.escape(project) + r'-(?:(?:db|backend)|update-[0-9a-f]{32}-(?:db|backend))@sha256:[0-9a-f]{64}', reference) is not None


def validate_inventory(value):
    """Return a detached, finite JSON proof; importing/calling performs no I/O."""
    _exact(value, ('format', 'version', 'project', 'installation', 'docker_host', 'operation_id', 'containers', 'volumes', 'networks', 'images'))
    _require(value['format'] == 'leti-cook-uninstall-resources' and type(value['version']) is int and value['version'] == 1)
    project, installation, operation = _project(value['project']), _path(value['installation']), _operation(value['operation_id'])
    _docker_host(value['docker_host'])
    containers, volumes, networks, images = (_list(value[name], maximum) for name, maximum in
                                             (('containers', 3), ('volumes', 4), ('networks', 2), ('images', 128)))
    for item in containers:
        _exact(item, ('id', 'name', 'kind', 'image', 'labels'))
        _identifier(item['id']); _identifier(item['image'], image=True)
        _require(item['kind'] in ('db', 'backend', 'helper'))
        labels = _labels(item['labels'], (PROJECT, SERVICE, ONEOFF, WORKDIR, OWN_PROJECT, OPERATION))
        if item['kind'] == 'helper':
            _require(operation is not None and item['name'] == project + '-update-' + operation + '-helper'
                     and labels == {OWN_PROJECT: project, OPERATION: operation}, 'uninstall_helper_not_owned')
        else:
            kind = item['kind']
            # Images may contribute ownership from a previous update, independent
            # of the current journal. Compose identity remains mandatory.
            _require(item['name'] == project + '-' + kind + '-1'
                     and {PROJECT, SERVICE, ONEOFF, WORKDIR} <= set(labels)
                     and (OWN_PROJECT not in labels or labels[OWN_PROJECT] == project)
                     and (OPERATION not in labels or labels.get(OWN_PROJECT) == project
                          and _OP.fullmatch(labels[OPERATION]) is not None)
                     and labels[PROJECT] == project and labels[SERVICE] == kind
                     and labels[ONEOFF].lower() == 'false'
                     and labels[WORKDIR] in (installation, installation + '/database'), 'uninstall_container_not_owned')
    _unique([item['id'] for item in containers]); _unique([item['kind'] for item in containers])
    for item in volumes:
        _exact(item, ('name', 'created_at', 'driver', 'options', 'labels'))
        name = _volume_name(item['name'], project)
        _string(item['created_at'], 96)
        _require(item['driver'] == 'local' and item['options'] == {} and type(item['options']) is dict, 'uninstall_volume_not_plain_local')
        labels = _labels(item['labels'], (PROJECT, VOLUME, OPERATION))
        logical = 'application-state' if name.endswith('_application-state') else 'database-data'
        expected = {PROJECT: project, VOLUME: logical}
        if '_restore_' in name:
            expected[OPERATION] = name.rsplit('_restore_', 1)[1]
        _require(labels == expected, 'uninstall_volume_not_owned')
    _unique([item['name'] for item in volumes])
    container_ids = {item['id'] for item in containers}
    for item in networks:
        _exact(item, ('id', 'name', 'driver', 'internal', 'labels', 'endpoints'))
        _identifier(item['id'])
        labels = _labels(item['labels'], (PROJECT, NETWORK))
        logical = labels.get(NETWORK)
        _require(logical in ('database', 'application-access') and labels == {PROJECT: project, NETWORK: logical}
                 and item['name'] == project + '_' + logical and item['driver'] == 'bridge'
                 and type(item['internal']) is bool and item['internal'] == (logical == 'database'), 'uninstall_network_not_owned')
        for ident in _list(item['endpoints'], 3):
            _identifier(ident)
        _unique(item['endpoints'])
        _require(set(item['endpoints']) <= container_ids, 'uninstall_network_has_foreign_endpoint')
    _unique([item['id'] for item in networks]); _unique([item['name'] for item in networks])
    for item in images:
        _exact(item, ('id', 'tags', 'repo_digests', 'labels', 'foreign_references'))
        _identifier(item['id'], image=True)
        for key in ('tags', 'repo_digests'):
            for reference in _list(item[key], 128):
                _require(type(reference) is str and _TAG.fullmatch(reference) is not None)
            _unique(item[key])
        labels = _labels(item['labels'], (PROJECT, OWN_PROJECT, OPERATION))
        # Images may legitimately belong to a different upstream project.
        # Keep its finite metadata so removal can explicitly retain it; a
        # foreign label never becomes a Docker argument or ownership proof.
        for ident in _list(item['foreign_references'], MAX_GLOBAL):
            _identifier(ident)
        _unique(item['foreign_references'])
        _require(not set(item['foreign_references']) & container_ids)
    _unique([item['id'] for item in images])
    return json.loads(json.dumps(value, separators=(',', ':')))


def _selected_labels(keys, expression):
    return '{' + ','.join(json.dumps(key) + ':{{json (index ' + expression + ' ' + json.dumps(key) + ')}}' for key in keys) + '}'


_CONTAINER_FORMAT = ('{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},'
    '"running":{{json .State.Running}},"labels":' + _selected_labels((PROJECT, SERVICE, ONEOFF, WORKDIR, OWN_PROJECT, OPERATION), '.Config.Labels') + '}')
_GLOBAL_FORMAT = ('{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},'
    '"volumes":[null{{range .Mounts}}{{if eq .Type "volume"}},{{json .Name}}{{end}}{{end}}]}')
_VOLUME_FORMAT = ('{"name":{{json .Name}},"created_at":{{json .CreatedAt}},"driver":{{json .Driver}},'
    '"options":{{json .Options}},"labels":' + _selected_labels((PROJECT, VOLUME, OPERATION), '.Labels') + '}')
_NETWORK_FORMAT = ('{"id":{{json .Id}},"name":{{json .Name}},"driver":{{json .Driver}},"internal":{{json .Internal}},'
    '"endpoints":[null{{range $id, $value := .Containers}},{{json $id}}{{end}}],"labels":' + _selected_labels((PROJECT, NETWORK), '.Labels') + '}')
_IMAGE_FORMAT = ('{"id":{{json .Id}},"tags":{{json .RepoTags}},"repo_digests":{{json .RepoDigests}},'
    '"labels":' + _selected_labels((PROJECT, OWN_PROJECT, OPERATION), '.Config.Labels') + '}')


class DockerResources:
    def __init__(self, project, installation, run=None):
        self.project = _project(project)
        self.installation = _path(str(installation))
        if run is None:
            from install import command
            run = command
        _require(callable(run), 'uninstall_runner_required')
        self.run = run
        self._endpoint = None

    def _invoke(self, args, *, timeout=30, endpoint=None):
        try:
            output = self.run(['docker', *(['--host', endpoint] if endpoint else []), *args], timeout=timeout)
        except Exception:
            raise ResourceError('uninstall_docker_command_failed') from None
        _require(type(output) is str and len(output.encode('utf-8')) <= MAX_OUTPUT, 'uninstall_docker_output_invalid')
        return output

    def _ensure_daemon(self, expected=None):
        # Lazy: terminal filesystem-only recovery does not require Docker.
        _require(sys.platform == 'linux', 'uninstall_linux_required')
        if expected is not None:
            _docker_host(expected)
            _require(self._endpoint in (None, expected), 'uninstall_docker_endpoint_changed')
        endpoint = self._endpoint or expected
        if endpoint is None:
            context = os.environ.get('DOCKER_CONTEXT', '')
            endpoint = os.environ.get('DOCKER_HOST', '')
            if context or not endpoint:
                args = ['context', 'inspect', '--format', '{{.Endpoints.docker.Host}}']
                if context:
                    _require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', context) is not None,
                             'uninstall_docker_context_invalid')
                    args.append(context)
                endpoint = self._invoke(args).strip()
        _docker_host(endpoint)
        try:
            system = json.loads(self._invoke(['info', '--format', '{{json .OSType}}'], endpoint=endpoint))
        except json.JSONDecodeError:
            raise ResourceError('uninstall_docker_metadata_invalid') from None
        _require(system == 'linux', 'uninstall_linux_docker_required')
        self._endpoint = endpoint

    def _docker(self, args, *, timeout=30):
        _require(self._endpoint is not None, 'uninstall_docker_guard_required')
        return self._invoke(args, timeout=timeout, endpoint=self._endpoint)

    def _names(self, kind, *, filters=()):
        args = {'container': ['container', 'ls', '--all', '--quiet', '--no-trunc'],
                'volume': ['volume', 'ls', '--quiet'], 'network': ['network', 'ls', '--quiet', '--no-trunc'],
                'image': ['image', 'ls', '--quiet', '--no-trunc']}[kind]
        for value in filters:
            args += ['--filter', value]
        values = self._docker(args).split()
        _require(len(values) <= MAX_GLOBAL, 'uninstall_inventory_too_large')
        for item in values:
            if kind in ('container', 'network'):
                _identifier(item)
            elif kind == 'image':
                _identifier(item, image=True)
            else:
                _require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', item) is not None)
        return sorted(set(values))

    def _inspect(self, kind, identity):
        template = {'container': _CONTAINER_FORMAT, 'volume': _VOLUME_FORMAT, 'network': _NETWORK_FORMAT, 'image': _IMAGE_FORMAT}[kind]
        try:
            value = json.loads(self._docker([kind, 'inspect', '--format', template, identity]))
        except (TypeError, json.JSONDecodeError):
            raise ResourceError('uninstall_docker_metadata_invalid') from None
        _require(type(value) is dict, 'uninstall_docker_metadata_invalid')
        if 'labels' in value:
            _require(type(value['labels']) is dict)
            value['labels'] = {key: item for key, item in value['labels'].items() if item not in (None, '')}
        if kind == 'container':
            _exact(value, ('id', 'name', 'image', 'running', 'labels'))
            _identifier(value['id']); _identifier(value['image'], image=True)
            _require(type(value['running']) is bool and type(value['name']) is str and value['name'].startswith('/'))
            value['name'] = value['name'][1:]
        if kind == 'volume' and value.get('options') is None:
            value['options'] = {}
        if kind == 'network':
            _require(type(value.get('endpoints')) is list and value['endpoints'][0:1] == [None])
            value['endpoints'] = sorted(value['endpoints'][1:])
        if kind == 'image':
            for key in ('tags', 'repo_digests'):
                value[key] = sorted(value.get(key) or [])
        return value

    def _global_containers(self):
        result = []
        ids = self._names('container')
        # Fixed small batches bound argv/output without requesting Env or bind paths.
        for start in range(0, len(ids), 64):
            batch = ids[start:start + 64]
            lines = self._docker(['container', 'inspect', '--format', _GLOBAL_FORMAT, *batch]).splitlines()
            _require(len(lines) == len(batch), 'uninstall_docker_metadata_invalid')
            for line in lines:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    raise ResourceError('uninstall_docker_metadata_invalid') from None
                _exact(value, ('id', 'name', 'image', 'volumes'))
                _identifier(value['id']); _identifier(value['image'], image=True)
                _require(value['id'] in batch)
                _string(value['name'])
                _list(value['volumes'], 129)
                _require(value['volumes'][0:1] == [None])
                value['volumes'] = value['volumes'][1:]
                for name in value['volumes']:
                    _require(type(name) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name) is not None)
                result.append(value)
        _unique([item['id'] for item in result])
        return result

    def _journal(self, journal):
        if journal is None:
            return None, set(), set()
        _require(type(journal) is dict and journal.get('project') == self.project, 'uninstall_update_journal_invalid')
        operation = journal.get('operation_id')
        _require(type(operation) is str and _OP.fullmatch(operation) is not None, 'uninstall_update_journal_invalid')
        images, volumes = set(), set()
        for key in ('old_images', 'new_images'):
            values = journal.get(key)
            if values is None and key == 'new_images':
                continue
            _exact(values, ('db', 'backend'))
            images.update(_identifier(image, image=True) for image in values.values())
        for key in ('old_volume', 'restore_volume'):
            name = journal.get(key)
            if name is not None:
                volumes.add(_volume_name(name, self.project))
        # A crash can occur after volume creation but before storing its receipt.
        volumes.add(self.project + '_restore_' + operation)
        return operation, images, volumes

    def _expected_network_ids(self):
        names = {self.project + '_' + logical for logical in ('database', 'application-access')}
        lines = self._docker(['network', 'ls', '--no-trunc', '--format', '{{.ID}} {{.Name}}']).splitlines()
        _require(len(lines) <= MAX_GLOBAL, 'uninstall_inventory_too_large')
        result = set()
        for line in lines:
            fields = line.split()
            _require(len(fields) == 2, 'uninstall_docker_metadata_invalid')
            _identifier(fields[0]); _string(fields[1])
            if fields[1] in names:
                result.add(fields[0])
        return result

    def _owned_container_ids(self, operation, global_items):
        ids = set(self._names('container', filters=('label=' + PROJECT + '=' + self.project,)))
        ids.update(self._names('container', filters=('label=' + OWN_PROJECT + '=' + self.project,)))
        names = {'/' + self.project + '-' + kind + '-1' for kind in ('db', 'backend')}
        if operation:
            names.add('/' + self.project + '-update-' + operation + '-helper')
        ids.update(item['id'] for item in global_items if item['name'] in names)
        return ids

    def inspect_plan(self, journal=None):
        operation, image_ids, allowed_volumes = self._journal(journal)
        self._ensure_daemon()
        global_items = self._global_containers()
        container_ids = self._owned_container_ids(operation, global_items)
        containers = []
        for ident in sorted(container_ids):
            value = self._inspect('container', ident)
            _require(value['id'] == ident, 'uninstall_container_identity_changed')
            value.pop('running')
            value['kind'] = value['labels'].get(SERVICE, 'helper')
            if value['kind'] == 'helper':
                _require(value['image'] in image_ids, 'uninstall_helper_image_not_recorded')
            containers.append(value)
            image_ids.add(value['image'])
        # Only the current DB mount or the verified journal admits restored names.
        for item in global_items:
            if any(c['id'] == item['id'] and c['kind'] == 'db' for c in containers):
                allowed_volumes.update(_volume_name(name, self.project) for name in item['volumes'])
        allowed_volumes.update((self.project + '_database-data', self.project + '_application-state'))
        names = set(self._names('volume', filters=('label=' + PROJECT + '=' + self.project,)))
        names.update(set(self._names('volume')) & allowed_volumes)
        _require(names <= allowed_volumes, 'uninstall_unrecorded_project_volume')
        volumes = []
        for name in sorted(names):
            volume = self._inspect('volume', name)
            _require(volume.get('name') == name, 'uninstall_volume_identity_changed')
            volumes.append(volume)
        for volume in volumes:
            _require(not any(volume['name'] in item['volumes'] and item['id'] not in container_ids for item in global_items),
                     'uninstall_volume_has_foreign_reference')
        network_ids = set(self._names('network', filters=('label=' + PROJECT + '=' + self.project,))) | self._expected_network_ids()
        networks = []
        for ident in sorted(network_ids):
            network = self._inspect('network', ident)
            _require(network.get('id') == ident, 'uninstall_network_identity_changed')
            networks.append(network)
        image_ids.update(self._names('image', filters=('label=' + OWN_PROJECT + '=' + self.project,)))
        image_ids.update(self._names('image', filters=('label=' + PROJECT + '=' + self.project,)))
        for kind in ('db', 'backend'):
            image_ids.update(self._names('image', filters=('reference=' + self.project + '-' + kind + ':latest',)))
        available_images = set(self._names('image'))
        images = []
        for ident in sorted(image_ids & available_images):
            value = self._inspect('image', ident)
            _require(value['id'] == ident, 'uninstall_image_identity_changed')
            value['foreign_references'] = sorted(item['id'] for item in global_items if item['image'] == ident and item['id'] not in container_ids)
            images.append(value)
        return validate_inventory({'format': 'leti-cook-uninstall-resources', 'version': 1,
            'project': self.project, 'installation': self.installation, 'docker_host': self._endpoint, 'operation_id': operation,
            'containers': containers, 'volumes': volumes, 'networks': networks, 'images': images})

    def _plan(self, value):
        plan = validate_inventory(value)
        _require((plan['project'], plan['installation']) == (self.project, self.installation), 'uninstall_plan_scope_mismatch')
        self._ensure_daemon(plan['docker_host'])
        return plan

    @staticmethod
    def _result():
        return {'removed': [], 'absent': [], 'retained': []}

    def _container(self, captured):
        if captured['id'] not in self._names('container'):
            return None
        current = self._inspect('container', captured['id'])
        running = current.pop('running')
        current['kind'] = current['labels'].get(SERVICE, 'helper')
        _require(current == captured, 'uninstall_container_identity_changed')
        return running

    def stop(self, value):
        plan, result = self._plan(value), self._result()
        result['stopped'] = []
        order = {'helper': 0, 'backend': 1, 'db': 2}
        for item in sorted(plan['containers'], key=lambda row: order[row['kind']]):
            running = self._container(item)
            if running is None:
                result['absent'].append(item['id']); continue
            self._docker(['update', '--restart=no', item['id']])
            running = self._container(item)
            if running:
                self._docker(['stop', '--time', '10', item['id']], timeout=25)
            _require(self._container(item) in (False, None), 'uninstall_container_still_running')
            result['stopped'].append(item['id'])
        return result

    def remove_containers(self, value):
        plan, result = self._plan(value), self._result()
        for item in plan['containers']:
            running = self._container(item)
            if running is None:
                result['absent'].append(item['id']); continue
            _require(not running, 'uninstall_container_still_running')
            self._docker(['container', 'rm', item['id']])
            _require(item['id'] not in self._names('container'), 'uninstall_container_removal_unconfirmed')
            result['removed'].append(item['id'])
        return result

    def remove_volumes(self, value):
        plan, result = self._plan(value), self._result()
        for item in plan['volumes']:
            if item['name'] not in self._names('volume'):
                result['absent'].append(item['name']); continue
            _require(self._inspect('volume', item['name']) == item, 'uninstall_volume_identity_changed')
            _require(not any(item['name'] in row['volumes'] for row in self._global_containers()), 'uninstall_volume_still_referenced')
            # Repeat identity after reference observation; Docker conflict is an
            # error, never permission to force-remove another root's resource.
            _require(self._inspect('volume', item['name']) == item, 'uninstall_volume_identity_changed')
            self._docker(['volume', 'rm', item['name']])
            _require(item['name'] not in self._names('volume'), 'uninstall_volume_removal_unconfirmed')
            result['removed'].append(item['name'])
        return result

    def remove_networks(self, value):
        plan, result = self._plan(value), self._result()
        for item in plan['networks']:
            if item['id'] not in self._names('network'):
                result['absent'].append(item['id']); continue
            current = self._inspect('network', item['id'])
            _require({k: v for k, v in current.items() if k != 'endpoints'} == {k: v for k, v in item.items() if k != 'endpoints'}, 'uninstall_network_identity_changed')
            _require(not current['endpoints'], 'uninstall_network_still_connected')
            self._docker(['network', 'rm', item['id']])
            _require(item['id'] not in self._names('network'), 'uninstall_network_removal_unconfirmed')
            result['removed'].append(item['id'])
        return result

    def _image_reason(self, captured, current, global_items):
        if any(item['image'] == current['id'] for item in global_items):
            return 'container_reference'
        if any(not _own_digest(reference, self.project)
               for reference in current['repo_digests'] + captured['repo_digests']):
            return 'published_digest'
        if any(not _own_tag(tag, self.project) for tag in current['tags'] + captured['tags']):
            return 'foreign_tag'
        labels = current['labels']
        if any(labels[key] != self.project for key in (PROJECT, OWN_PROJECT) if key in labels):
            return 'foreign_owner'
        if OPERATION in labels and _OP.fullmatch(labels[OPERATION]) is None:
            return 'ownership_unproven'
        if not (captured['tags'] or labels.get(PROJECT) == self.project or labels.get(OWN_PROJECT) == self.project):
            return 'ownership_unproven'
        return None

    def remove_images(self, value):
        plan, result = self._plan(value), self._result()
        for item in plan['images']:
            ident = item['id']
            if ident not in self._names('image'):
                result['absent'].append(ident); continue
            while ident in self._names('image'):
                current = self._inspect('image', ident)
                _require(current['id'] == ident and current['labels'] == item['labels'], 'uninstall_image_identity_changed')
                reason = self._image_reason(item, current, self._global_containers())
                if reason:
                    result['retained'].append({'id': ident, 'reason': reason}); break
                _require(set(current['tags']) <= set(item['tags']), 'uninstall_image_tags_changed')
                reference = current['tags'][0] if current['tags'] else ident
                # No -f and no parent pruning. A current image reference/tag
                # conflict stops this operation; other images are untouched.
                self._docker(['image', 'rm', '--no-prune', reference])
                if ident not in self._names('image'):
                    result['removed'].append(ident); break
                after = self._inspect('image', ident)
                _require(reference not in after['tags'] and reference != ident, 'uninstall_image_removal_unconfirmed')
        return result

    def verify_removed(self, value):
        plan = self._plan(value)
        remaining_containers = self._owned_container_ids(plan['operation_id'], self._global_containers())
        _require(not remaining_containers, 'uninstall_project_container_remaining')
        names = set(self._names('volume'))
        _require(not any(item['name'] in names for item in plan['volumes']), 'uninstall_project_volume_remaining')
        _require(not self._names('volume', filters=('label=' + PROJECT + '=' + self.project,)), 'uninstall_project_volume_remaining')
        _require(not self._names('network', filters=('label=' + PROJECT + '=' + self.project,)), 'uninstall_project_network_remaining')
        _require(not self._expected_network_ids(), 'uninstall_project_network_remaining')
        ids = set(self._names('network'))
        _require(not any(item['id'] in ids for item in plan['networks']), 'uninstall_project_network_remaining')
        return {'complete': True}
