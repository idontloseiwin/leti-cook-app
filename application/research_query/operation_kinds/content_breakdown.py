"""video.content_breakdown.v1: the Overview content breakdown of one period.

Over the fingerprint's population and period: official categories, YouTube
topics (multi-label, shares overlap), declared audio and metadata languages,
caption availability and catalogue age. A value that is not collected or stale
is unknown and counted per section. A single-label share is count / known rows;
a topic share is count / rows with collected topics. Groups are
ordered by count, then key; at most GROUP_LIMIT per section, the rest counted.
"""
from urllib.parse import unquote, urlparse

from ..registry import SOURCE_ID
from . import overview
from .kind import OperationDefinition, OperationKind, population_provenance

ID = 'video.content_breakdown.v1'
GROUP_LIMIT = 250
SECTIONS = {
    'official_categories': 'youtube.video.snippet.categoryId',
    'youtube_topics': 'youtube.video.topicDetails.topicCategories',
    'audio_languages': 'youtube.video.snippet.defaultAudioLanguage',
    'metadata_languages': 'youtube.video.snippet.defaultLanguage',
    'captions': 'youtube.video.contentDetails.caption',
    'catalogue_age': 'leti_cook.video_age_from_published_at',
}
FIXED_LABELS = {
    'captions': {'available': 'Captions available', 'unavailable': 'No captions'},
    'catalogue_age': {'under_1y': 'Under 1 year', '1_3y': '1-3 years', '3_5y': '3-5 years', '5y_plus': '5+ years'},
}
FIELDS = ('video.category_id', 'video.topic_urls', 'video.audio_language', 'video.metadata_language',
          'video.caption_available', 'video.published_at')

DEFINITION = OperationDefinition(ID, 'public.videos.current.v1', 'video', 'groups', 'descriptive_analytics', (),
    'Official categories, YouTube topics, declared languages, caption availability and catalogue age of the public '
    'videos of one type published in a rolling period; unknown counted per section, topics overlap.')


def dependencies(params, source_id, scope_kind):
    if source_id != SOURCE_ID:
        raise ValueError()
    overview.accept(params, ('period',))
    return FIELDS


def topic_label(value):
    """Use the topic URL’s final path segment as its label."""
    slug = unquote(urlparse(value).path.rsplit('/', 1)[-1]).replace('_', ' ').strip()
    return slug or value


def _labels(section, keys):
    """Display labels and the earliest expiry of a label read outside SQL."""
    if section == 'official_categories':
        from source_expiry import captured_source_call, minimum_expiry
        from youtube_categories import youtube_video_category_name
        labels, expiries = {}, []
        for key in keys:
            labels[key], expiry = captured_source_call(youtube_video_category_name, key)
            expiries.append(expiry)
        return labels, minimum_expiry(*expiries)
    if section == 'youtube_topics':
        return {key: topic_label(key) for key in keys}, None
    if section in FIXED_LABELS:
        if set(keys) - set(FIXED_LABELS[section]):
            raise ValueError()
        return FIXED_LABELS[section], None
    return {key: key for key in keys}, None


def check(result):
    if set(result) != {'period', 'population', 'sample_count', 'known', 'topics_empty', 'groups'}:
        raise ValueError()
    overview.check_period(result['period'])
    population = overview.check_population(result['population'])
    sample = overview.count(result['sample_count'])
    known, groups = result['known'], result['groups']
    if sample != population['matched'] or set(known) != set(SECTIONS) or set(groups) != set(SECTIONS):
        raise ValueError()
    sections = {}
    for name, source in SECTIONS.items():
        known_count = overview.count(known[name])
        items, group_count = groups[name]['items'], overview.count(groups[name]['group_count'])
        if known_count > sample or len(items) > min(group_count, GROUP_LIMIT) or (group_count > len(items) and len(items) < GROUP_LIMIT):
            raise ValueError()
        keys = [item['key'] for item in items]
        if any(type(key) is not str or not key for key in keys) or len(set(keys)) != len(keys):
            raise ValueError()
        labels, label_expiry = _labels(name, keys)
        multi = name == 'youtube_topics'
        section = dict(kind='multi_label' if multi else 'single_label', source=source,
                       share_basis='collected_rows' if multi else 'known_rows',
                       sample_count=sample, known_count=known_count, missing_count=sample - known_count,
                       groups=[dict(key=item['key'], label=labels[item['key']], count=overview.count(item['count']),
                                    share_percent=overview.share(item['count'], known_count)) for item in items],
                       omitted_group_count=group_count - len(items))
        if not multi and sum(g['count'] for g in section['groups']) > known_count:
            raise ValueError()
        if multi:
            empty = overview.count(result['topics_empty'])
            if empty > known_count:
                raise ValueError()
            section.update(assigned_count=known_count - empty, empty_count=empty)
        if name == 'official_categories':
            section['label_expires_at'] = label_expiry
        sections[name] = section
    return dict(period=result['period'], population=population, **sections)


def lifetime(result):
    """Category labels come from the managed reference; its expiry bounds the result."""
    return result['official_categories']['label_expires_at']


KIND = OperationKind(DEFINITION, dependencies, check, population_provenance('calculated'), lifetime=lifetime)
