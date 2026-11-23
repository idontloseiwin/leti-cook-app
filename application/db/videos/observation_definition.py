"""Definition attached by the existing completion writer, never a read backfill.

YouTube videos.statistics.viewCount: all-format play starts from 2026-08-24.
Official source: https://developers.google.com/youtube/data/reference/rest/v3/videos#statistics.viewCount
Unknown earlier input stays unclassified. Changing this
mapping does not rewrite retained observations or change collection cadence.
"""
def view_epoch_sql(clock):
    # Only internal fixed writer expressions may call this SQL constructor.
    if clock != 'input.recorded_at':
        raise ValueError('unsupported_observation_clock')
    return "CASE WHEN input.recorded_at>='2026-08-24T00:00:00Z'::timestamptz THEN 'youtube.views.play_start.20260824' END"
