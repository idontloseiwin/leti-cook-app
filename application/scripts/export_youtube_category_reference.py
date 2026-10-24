#!/usr/bin/env python3
"""Explicit one-shot export of official YouTube category labels; never a runtime refresh."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from youtube_client import YOUTUBE_DATA_API_BASE_URL, valid_metadata_etag

MAX_LANGUAGES = 200
MAX_CATEGORY_IDS = 100
LANGUAGE_CODE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
SOURCE = {
    "languages_url": f"{YOUTUBE_DATA_API_BASE_URL}/i18nLanguages",
    "categories_url": f"{YOUTUBE_DATA_API_BASE_URL}/videoCategories",
}


class CategoryReferenceError(ValueError):
    pass


def category_ids(value):
    ids = value.split(',')
    if not 1 <= len(ids) <= MAX_CATEGORY_IDS or len(set(ids)) != len(ids):
        raise CategoryReferenceError('invalid_category_ids')
    if any(not re.fullmatch(r'[1-9][0-9]{0,8}', item) for item in ids):
        raise CategoryReferenceError('invalid_category_ids')
    return sorted(ids, key=int)


@dataclass(frozen=True)
class ReferenceResponse:
    payload: dict | None
    etag: str | None = None


def entity_tag(value):
    """One validator in the bare form YouTube compares If-None-Match with, or None.

    Response bodies carry the tag bare and only that form gets a 304. Tags an
    earlier release stored in quotes are unquoted.
    """
    if isinstance(value, str) and len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value if valid_metadata_etag(value) else None


def response_etag(result, payload):
    """The response's ETag header, else the tag in its body."""
    headers = getattr(result, 'headers', None) or {}
    return entity_tag(headers.get('ETag')) or entity_tag(payload.get('etag'))


def export_reference(get, ids, *, now=None, progress=None, previous=None, clock=None):
    # The first response must not inherit the end time of a long export.
    acquired_at = now or datetime.now(timezone.utc)
    from youtube_category_reference import validate_reference
    # Reject stale caches before issuing a conditional request. No external seed
    # becomes fresh merely because it was copied into the application directory.
    ids = category_ids(','.join(ids))
    if previous is not None:
        previous = deepcopy(previous)
        try:
            validate_reference(previous, now=acquired_at)
        except (ValueError, TypeError, OverflowError):
            previous = None
    validators = (previous or {}).get('validators', {})
    if not isinstance(validators, dict):
        validators = {}
    same_ids = validators.get('category_ids') == ids
    old_categories = validators.get('categories', {})
    if not isinstance(old_categories, dict):
        old_categories = {}
    new_validators = {'category_ids': ids, 'languages': None, 'categories': {}}
    reused = False

    def fetch(endpoint, params, etag, cached):
        nonlocal reused
        # A payload must still be admitted at request time, not just at startup.
        if previous is not None:
            try:
                validate_reference(previous, now=clock() if clock else acquired_at)
            except (ValueError, TypeError, OverflowError):
                etag = None
        etag = entity_tag(etag)
        conditional = etag is not None and cached is not None
        response = get(endpoint, params, if_none_match=etag) if conditional else get(endpoint, params)
        if not isinstance(response, ReferenceResponse):
            response = ReferenceResponse(response, response.get('etag') if isinstance(response, dict) else None)
        if response.payload is None:
            if not conditional:
                raise CategoryReferenceError('unexpected_not_modified')
            validate_reference(previous, now=clock() if clock else acquired_at)
            reused = True
            # A 304 validates precisely the cached representation of this request.
            return cached, entity_tag(response.etag) or etag
        if not isinstance(response.payload, dict) or response.payload.get('error'):
            raise CategoryReferenceError('invalid_provider_response')
        return response.payload, entity_tag(response.etag)

    cached_languages = None if previous is None else {
        'items': [{'snippet': {'hl': code, 'name': name}} for code, name in previous['languages'].items()]}
    response, new_validators['languages'] = fetch(
        'i18nLanguages', {'part': 'snippet', 'hl': 'en'}, validators.get('languages'), cached_languages)

    items = response.get('items')
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_LANGUAGES:
        raise CategoryReferenceError('invalid_language_list')
    languages = {}
    for item in items:
        snippet = item.get('snippet') or {}
        code, name = snippet.get('hl'), snippet.get('name')
        if not isinstance(code, str) or not LANGUAGE_CODE.fullmatch(code):
            raise CategoryReferenceError('invalid_language_code')
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise CategoryReferenceError('invalid_language_name')
        if code.casefold() in {existing.casefold() for existing in languages}:
            raise CategoryReferenceError('duplicate_language')
        languages[code] = name.strip()
    if 'en' not in languages:
        raise CategoryReferenceError('missing_english_language')
    allowed = set(ids)
    categories = {}
    for code in ['en', *sorted(set(languages) - {'en'})]:
        cached_labels = (previous or {}).get('categories', {}).get(code)
        cached = None if not same_ids or cached_labels is None else {
            'items': [{'id': identity, 'snippet': {'title': title}} for identity, title in cached_labels.items()]}
        response, new_validators['categories'][code] = fetch(
            'videoCategories', {'part': 'snippet', 'id': ','.join(ids), 'hl': code},
            old_categories.get(code) if same_ids else None, cached)

        rows = response.get('items')
        if not isinstance(rows, list) or not rows or len(rows) > len(ids):
            raise CategoryReferenceError('invalid_category_list')
        labels = {}
        for row in rows:
            identity, name = row.get('id'), (row.get('snippet') or {}).get('title')
            if identity not in allowed or identity in labels:
                raise CategoryReferenceError('invalid_category_identity')
            if not isinstance(name, str) or not name.strip() or len(name) > 300:
                raise CategoryReferenceError('invalid_category_label')
            labels[identity] = name.strip()
        if code == 'en' and set(labels) != allowed:
            raise CategoryReferenceError('incomplete_english_categories')
        categories[code] = labels
        if progress is not None:
            progress(len(categories), len(languages))
    # A failed/incomplete cycle never installs or advances the reference clock.
    # Do not revive data that expired while other locales were being fetched.
    if reused:
        validate_reference(previous, now=clock() if clock else acquired_at)
    return {
        'validators': new_validators,
        'schema_version': 1,
        'retrieved_at': acquired_at.isoformat().replace('+00:00', 'Z'),
        'clock_origin': 'acquisition_started_at',
        'source': SOURCE,
        'languages': languages,
        'categories': categories,
    }


def write_reference(output, reference):
    """Publish a complete export without replacing an existing file."""
    output = Path(output)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=output.parent, prefix='.category-export-', delete=False) as stream:
            pending = Path(stream.name)
            json.dump(reference, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.link(pending, output)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch', action='store_true', help='explicitly send bounded provider GETs')
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument('--output', type=Path, help='new external export file; never overwritten')
    destination.add_argument('--install', action='store_true', help='atomically replace the managed self-hosted reference')
    parser.add_argument('--category-ids', required=True, help='reviewed existing IDs to preserve')
    parser.add_argument('--app-root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    recorder = None
    resources = None
    requests_sent = 0
    application_data_writes = False
    try:
        ids = category_ids(args.category_ids)
        if os.getenv("LIC_DEPLOYMENT_PROFILE", "private").strip() == "self-hosted" and not args.install:
            raise CategoryReferenceError("self_hosted_requires_managed_install")
        if args.output is not None and (args.output.exists() or not args.output.parent.is_dir()):
            raise CategoryReferenceError('output_must_be_new_in_existing_directory')
        if not args.fetch:
            print(json.dumps({'status': 'planned', 'maximum_requests': MAX_LANGUAGES + 1,
                              'category_ids': len(ids), 'application_data_writes': application_data_writes}))
            return 0
        app_root = args.app_root.resolve(strict=True)
        if not (app_root / 'core.py').is_file():
            raise CategoryReferenceError('invalid_application_root')
        sys.path.insert(0, str(app_root))
        from core import API_KEY, YOUTUBE_QUOTA_ATTEMPT_RECORDER, process_database_resources, database
        resources = process_database_resources()
        resources.__enter__()
        from youtube_client import youtube_api_get
        recorder = YOUTUBE_QUOTA_ATTEMPT_RECORDER
        if not API_KEY:
            raise CategoryReferenceError('missing_api_key')

        def get(endpoint, params, *, if_none_match=None):
            nonlocal requests_sent
            if requests_sent >= MAX_LANGUAGES + 1:
                raise CategoryReferenceError('request_bound_exceeded')
            requests_sent += 1
            result = youtube_api_get(endpoint, phase='category_metadata', quota_bucket='general',
                                     attempt_recorder=recorder, params=params, api_key=API_KEY,
                                     timeout_seconds=20, quota_units=1, if_none_match=if_none_match)
            if result.status_code == 304:
                return ReferenceResponse(None)
            if result.status_code != 200:
                raise CategoryReferenceError(f'provider_http_{result.status_code}')
            payload = result.json()
            if not isinstance(payload, dict) or payload.get('error'):
                raise CategoryReferenceError('invalid_provider_response')
            return ReferenceResponse(payload, response_etag(result, payload))

        def progress(done, total):
            if done == 1 or done % 10 == 0 or done == total:
                print(json.dumps({'status': 'fetching', 'languages_done': done,
                                  'languages_total': total, 'requests_sent': requests_sent}), flush=True)

        previous = repository = None
        if args.install:
            from db.category_reference import CategoryReferenceRepository
            from youtube_category_reference import load_reference_for_refresh
            repository = CategoryReferenceRepository(database)
            previous = load_reference_for_refresh(repository=repository)
        reference = export_reference(get, ids, progress=progress, previous=previous,
                                     clock=lambda: datetime.now(timezone.utc))
        if args.install:
            from youtube_category_reference import install_reference
            application_data_writes = True
            install_reference(reference, repository=repository)
        else:
            write_reference(args.output, reference)
        print(json.dumps({'status': 'ok', 'languages': len(reference['languages']),
                          'category_ids': len(ids), 'requests_sent': requests_sent,
                          'labels': sum(len(rows) for rows in reference['categories'].values()),
                          'missing_localized_labels': sum(len(ids)-len(rows) for rows in reference['categories'].values()),
                          'application_data_writes': application_data_writes, 'quota_observed': True}), flush=True)
        return 0
    except Exception as error:
        # Never include response bodies, URLs with keys, environment values or tracebacks.
        code = str(error) if isinstance(error, CategoryReferenceError) else type(error).__name__
        print(json.dumps({'status': 'error', 'error_type': code, 'requests_sent': requests_sent,
                          'application_data_writes': application_data_writes}), flush=True)
        return 2
    finally:
        if resources is not None:
            try:
                resources.__exit__(None, None, None)
            except Exception as error:
                print(json.dumps({'status': 'warning', 'operation': 'quota_recorder_close',
                                  'error_type': type(error).__name__}), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
