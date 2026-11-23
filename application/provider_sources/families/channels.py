"""channels.list hook: a hidden subscriber count is not a public value.

``youtube_channel_counter_values`` owns the rule (hiddenSubscriberCount true ->
subscribers unknown). When it withholds a count the row reports OWNER_ONLY
instead of the number YouTube still sends.
"""
from youtube_channel_source_fields import youtube_channel_counter_values

from ..results import FieldValue, ValueState
from .base import Hook

SUBSCRIBERS = "statistics.subscriberCount"


class ChannelsList(Hook):
    def finish_row(self, method, item, fields):
        value = fields.get(SUBSCRIBERS)
        statistics = item.get("statistics")
        if value is None or value.state is not ValueState.KNOWN or not isinstance(statistics, dict):
            return fields
        public = youtube_channel_counter_values(statistics)["subscribers"]
        unhidden = youtube_channel_counter_values({**statistics, "hiddenSubscriberCount": False})["subscribers"]
        if public is None and unhidden is not None:   # withheld only because it is hidden
            fields = dict(fields)
            fields[SUBSCRIBERS] = FieldValue(ValueState.OWNER_ONLY, None, "owner_only")
        return fields
