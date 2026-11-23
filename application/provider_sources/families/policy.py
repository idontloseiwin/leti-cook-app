"""Validate method-specific parameter combinations. Execution requires a registered family and a parser. Refusals identify the parameter without including its value."""
from __future__ import annotations

from datetime import date, datetime
import re

from ..errors import PlanRejected

EXECUTABLE = frozenset({
    "youtube.videos.list", "youtube.videos.batchGetStats", "youtube.channels.list",
    "youtube.playlistItems.list", "youtube.playlists.list",
    # Owner OAuth: request builders and parsers verified on documented samples only;
    # there is no Google sign-in, so over HTTP every owner context answers owner_auth_unavailable.
    "youtube.captions.list", "youtube.captions.download", "youtube.liveBroadcasts.list",
    "youtube.liveChatModerators.list", "youtube.liveStreams.list", "youtube.members.list",
    "youtube.membershipsLevels.list", "youtube.playlistImages.list", "youtube.superChatEvents.list",
    "youtube.videoAbuseReportReasons.list", "youtube.videos.getRating", "youtube.subscriptions.list",
    "youtubeAnalytics.reports.query", "youtubeAnalytics.groups.list", "youtubeAnalytics.groupItems.list",
    "youtubereporting.reportTypes.list", "youtubereporting.jobs.list", "youtubereporting.jobs.get",
    "youtubereporting.jobs.reports.list", "youtubereporting.jobs.reports.get", "youtubereporting.media.download",
    # Public key families.
    "youtube.i18nLanguages.list", "youtube.i18nRegions.list", "youtube.videoCategories.list",
    "youtube.channelSections.list", "youtube.activities.list", "youtube.subscriptions.list",
    "youtube.commentThreads.list", "youtube.comments.list", "youtube.search.list",
    "youtube.liveChatMessages.list",
})

# Parts the layer refuses for a method, with the fixed reason code.
FORBIDDEN_PARTS = {
    # Counters of a one-off acquisition come from videos.batchGetStats; the
    # transport boundary keeps statistics out of videos.list.
    "youtube.videos.list": {"statistics": "statistics_from_batch_get_stats"},
}

# Parts that need a special or partner scope: never in a supported context.
SPECIAL_SCOPE_PARTS = {
    "youtube.channels.list": frozenset({"auditDetails", "contentOwnerDetails"}),
}

# Filters that address exactly one resource: one request, no page loop, and an
# empty answer in the expected envelope means "not found" (reconnect_source rule).
SINGLE_RESULT_FILTERS = {
    "youtube.channels.list": frozenset({"forHandle", "forUsername"}),
}

# Row objects returned whatever ``part`` says (not a part value): search hits always carry
# ``id`` (kind plus the matching ID), and the hit identity depends on it.
ALWAYS_RETURNED_PARTS = {
    "youtube.search.list": frozenset({"id"}),
}

# Top-level response keys a parse hook consumes besides the generic envelope.
ENVELOPE_EXTRAS = {
    "youtube.videos.batchGetStats": ("summary",),
    "youtube.search.list": ("regionCode",),
    "youtube.liveChatMessages.list": ("offlineAt", "pollingIntervalMillis"),
}

# Proto-JSON answers omit an empty list: {} (or only nextPageToken) is the empty page.
EMPTY_LIST_OMITTED = frozenset({
    "youtubereporting.reportTypes.list", "youtubereporting.jobs.list", "youtubereporting.jobs.reports.list",
    "youtubeAnalytics.groups.list", "youtubeAnalytics.groupItems.list",
})

# Parameters only the layer sets: the Analytics page index and the Reporting media path.
LAYER_MANAGED = {
    "youtubeAnalytics.reports.query": frozenset({"startIndex"}),
    # The media path comes only from jobs.reports.get downloadUrl (a capability): never from a client.
    "youtubereporting.media.download": frozenset({"resourceName"}),
}

# Inputs of a layer chain (not provider parameters) and their identity kinds.
CHAIN_INPUTS = {
    "youtubereporting.media.download": {"jobId": "reporting_job", "reportId": "reporting_report",
                                        "reportTypeId": "report_type"},
}
CHAIN_TARGETS = {"youtubereporting.media.download": "reportId"}

# Bounded media readers (not JSON): one body, read with byte, line and row limits.
MEDIA_READERS = {"youtube.captions.download": "caption_file", "youtubereporting.media.download": "report_csv"}

# A mode whose page token is a poll position: one page per acquisition, never "complete".
POLLING_MODES = {"youtube.members.list": ("mode", "updates")}

# Methods whose data belongs to the owner binding only: their rows never leave the owner context.
OWNER_ONLY_FAMILIES = frozenset({
    "youtube.captions.list", "youtube.captions.download", "youtube.liveBroadcasts.list",
    "youtube.liveChatModerators.list", "youtube.liveStreams.list", "youtube.members.list",
    "youtube.membershipsLevels.list", "youtube.playlistImages.list", "youtube.superChatEvents.list",
    "youtube.videoAbuseReportReasons.list", "youtube.videos.getRating",
    "youtubeAnalytics.reports.query", "youtubeAnalytics.groups.list", "youtubeAnalytics.groupItems.list",
    "youtubereporting.reportTypes.list", "youtubereporting.jobs.list", "youtubereporting.jobs.get",
    "youtubereporting.jobs.reports.list", "youtubereporting.jobs.reports.get", "youtubereporting.media.download",
})
# Methods that are polled, not paged: one request per acquisition; the next poll is a
# new explicit action with the continuation, not before pollingIntervalMillis.
POLLED_METHODS = frozenset({"youtube.liveChatMessages.list"})

# Documented refusals about the target of a request beyond the generic table
# Reason -> (entity state, parameter that names the target, or
# None for the method's filter). A 404 for an ID batch names no single ID: every ID
# of that batch becomes "omitted" (acquire.py), never "not found".
TARGET_REFUSALS = {
    # https://developers.google.com/youtube/v3/docs/channelSections/list#errors
    "youtube.channelSections.list": {"channelSectionForbidden": ("access_denied", None),
                                     "channelSectionNotFound": ("not_found", None)},
    # https://developers.google.com/youtube/v3/docs/commentThreads/list#errors
    "youtube.commentThreads.list": {"commentsDisabled": ("disabled", "videoId"),
                                    "videoNotFound": ("not_found", "videoId"),
                                    "channelNotFound": ("not_found", "allThreadsRelatedToChannelId"),
                                    "commentThreadNotFound": ("not_found", None)},
    # https://developers.google.com/youtube/v3/docs/comments/list#errors
    "youtube.comments.list": {"commentNotFound": ("not_found", None)},
    # https://developers.google.com/youtube/v3/docs/subscriptions/list#errors
    "youtube.subscriptions.list": {"subscriberNotFound": ("not_found", None),
                                   "accountClosed": ("access_denied", None),
                                   "accountSuspended": ("access_denied", None)},
    # https://developers.google.com/youtube/v3/docs/playlistItems/list#errors
    "youtube.playlistItems.list": {"videoNotFound": ("not_found", "videoId"),
                                   "playlistOperationUnsupported": ("access_denied", None)},
    # https://developers.google.com/youtube/v3/live/docs/liveChatMessages/list#errors
    "youtube.liveChatMessages.list": {"liveChatDisabled": ("disabled", "liveChatId"),
                                      "liveChatEnded": ("ended", "liveChatId"),
                                      "liveChatNotFound": ("not_found", "liveChatId"),
                                      "forbidden": ("access_denied", "liveChatId")},
}

# Documented request refusals with their own stop code (instead of request_invalid).
STOP_REASONS = {
    # "The id filter is only compatible with comments based on Google+."
    "youtube.comments.list": {"operationNotSupported": "operation_not_supported"},
    "youtube.commentThreads.list": {"operationNotSupported": "operation_not_supported"},
    # https://developers.google.com/youtube/v3/docs/search/list#errors
    "youtube.search.list": {"invalidSearchFilter": "search_filter_invalid"},
    # https://developers.google.com/youtube/v3/docs/videos/list#errors
    "youtube.videos.list": {"videoChartNotFound": "chart_not_available"},
}


# ---------------------------------------------------------------- parameter rules

_REGION = re.compile(r"[A-Za-z]{2}\Z")                                  # ISO 3166-1 alpha-2
_LANGUAGE = re.compile(r"[A-Za-z]{2,3}([_-][A-Za-z0-9]{1,8}){0,3}\Z")   # i18nLanguages ids: en, en_US, zh-Hant, es-419
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_LOCATION = re.compile(r"(-?[0-9]{1,3}(?:\.[0-9]{1,12})?),(-?[0-9]{1,3}(?:\.[0-9]{1,12})?)\Z")
_RADIUS = re.compile(r"([0-9]{1,9}(?:\.[0-9]{1,6})?)(m|km|ft|mi)\Z")
_METERS = {"m": 1.0, "km": 1000.0, "ft": 0.3048, "mi": 1609.344}
MAX_RADIUS_METERS = 1_000_000          # search.list: "values larger than 1000 kilometers are not supported"

# Formats every method shares by parameter name.
COMMON_FORMATS = {"regionCode": "region", "hl": "language"}
FORMATS = {
    # https://developers.google.com/youtube/v3/docs/search/list
    "youtube.search.list": {"relevanceLanguage": "language", "publishedAfter": "datetime",
                            "publishedBefore": "datetime", "location": "location", "locationRadius": "radius"},
    # https://developers.google.com/youtube/v3/docs/activities/list: a day alone is accepted
    # (publishedAfter includes that day, publishedBefore excludes it).
    "youtube.activities.list": {"publishedAfter": "datetime_or_date", "publishedBefore": "datetime_or_date"},
}
# search.list: "If you specify a value for this parameter, you must also set the type
# parameter's value to video."
SEARCH_VIDEO_ONLY = ("eventType", "location", "videoCaption", "videoCategoryId", "videoDefinition", "videoDimension",
                     "videoDuration", "videoEmbeddable", "videoLicense", "videoPaidProductPlacement",
                     "videoSyndicated", "videoType")
# Repeated parameters that are not ID batches and whose count the reference does not state: app bounds.
LIST_LIMITS = {"youtube.subscriptions.list": {"forChannelId": 50}}


def _datetime(text):
    if "T" not in text or len(text) > 64:
        return False
    try:
        moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return False
    return moment.tzinfo is not None and moment.utcoffset() is not None


def _day(text):
    if _DATE.fullmatch(text) is None:
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _location(text):
    match = _LOCATION.fullmatch(text)
    return match is not None and abs(float(match.group(1))) <= 90 and abs(float(match.group(2))) <= 180


def _radius(text):
    match = _RADIUS.fullmatch(text)
    return match is not None and 0 < float(match.group(1)) * _METERS[match.group(2)] <= MAX_RADIUS_METERS


_CHECKS = {
    "region": lambda text: _REGION.fullmatch(text) is not None,
    "language": lambda text: _LANGUAGE.fullmatch(text) is not None,
    "datetime": _datetime,
    "datetime_or_date": lambda text: _datetime(text) or _day(text),
    "location": _location,
    "radius": _radius,
}


def check_parameters(method_id, values):
    """``values``: parameter name -> tuple of canonical texts (after the generic checks)."""
    formats = dict(COMMON_FORMATS, **FORMATS.get(method_id, {}))
    for name, texts in values.items():
        kind = formats.get(name)
        if kind is not None and not all(_CHECKS[kind](text) for text in texts):
            raise PlanRejected("parameter_invalid", name)
    for name, limit in LIST_LIMITS.get(method_id, {}).items():
        if len(values.get(name, ())) > limit:
            raise PlanRejected("parameter_too_many_values", name)
    if method_id == "youtube.search.list":
        for name in SEARCH_VIDEO_ONLY:
            if name in values and values.get("type") != ("video",):
                raise PlanRejected("parameter_needs_type_video", name)
        if ("location" in values) != ("locationRadius" in values):
            raise PlanRejected("parameter_needs_pair", "location" if "location" not in values else "locationRadius")
