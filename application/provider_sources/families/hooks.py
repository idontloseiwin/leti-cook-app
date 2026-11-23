"""Parse hook of every executable method (policy.EXECUTABLE must match; a test keeps them equal)."""
from ..owner_sources import OwnerFamily, ReportsList
from .channels import ChannelsList
from .playlists import PlaylistItemsList, PlaylistsList
from .public import LiveChatMessagesList, PlainList, SearchList
from .videos import VideoBatchStats, VideosList

HOOKS = {
    "youtube.videos.list": VideosList(),
    "youtube.videos.batchGetStats": VideoBatchStats(),
    "youtube.channels.list": ChannelsList(),
    "youtube.playlistItems.list": PlaylistItemsList(),
    "youtube.playlists.list": PlaylistsList(),
    # Public key families.
    "youtube.i18nLanguages.list": PlainList(),
    "youtube.i18nRegions.list": PlainList(),
    "youtube.videoCategories.list": PlainList(),
    "youtube.channelSections.list": PlainList(),
    "youtube.activities.list": PlainList(),
    "youtube.subscriptions.list": PlainList(),     # owner filters (mine, mySubscribers, ...) too (g2)
    "youtube.commentThreads.list": PlainList(),
    "youtube.comments.list": PlainList(),
    "youtube.search.list": SearchList(),
    "youtube.liveChatMessages.list": LiveChatMessagesList(),
    # Owner OAuth, Analytics, Reporting: registry-only unless named here.
    **{method_id: OwnerFamily() for method_id in (
        "youtube.captions.list", "youtube.captions.download", "youtube.liveBroadcasts.list",
        "youtube.liveChatModerators.list", "youtube.liveStreams.list", "youtube.members.list",
        "youtube.membershipsLevels.list", "youtube.playlistImages.list", "youtube.superChatEvents.list",
        "youtube.videoAbuseReportReasons.list", "youtube.videos.getRating",
        "youtubeAnalytics.reports.query", "youtubeAnalytics.groups.list", "youtubeAnalytics.groupItems.list",
        "youtubereporting.reportTypes.list", "youtubereporting.jobs.list", "youtubereporting.jobs.get",
        "youtubereporting.jobs.reports.get", "youtubereporting.media.download")},
    "youtubereporting.jobs.reports.list": ReportsList(),
}


def hook_for(method_id):
    return HOOKS[method_id]
