"""Playlist and playlist membership response hooks.

playlists.list needs no hook: rows are playlists (unit ``playlist``), pages use
``pageToken`` and every value rule comes from the registry. playlistItems.list
rows are memberships; ``typed_playlist.playlist_video_item`` owns the rule for
the video reference, so a row it would refuse keeps no video reference here.
"""
from typed_playlist import playlist_video_item

from ..results import FieldValue, ValueState
from .base import Hook

VIDEO_FIELDS = ("contentDetails.videoId", "contentDetails.videoPublishedAt")


class PlaylistsList(Hook):
    pass


class PlaylistItemsList(Hook):
    def finish_row(self, method, item, fields):
        video = fields.get(VIDEO_FIELDS[0])
        if video is None or video.state is not ValueState.KNOWN or playlist_video_item(item) is not None:
            return fields
        fields = dict(fields)
        for path in VIDEO_FIELDS:
            if path in fields:
                fields[path] = FieldValue(ValueState.INVALID)
        return fields
