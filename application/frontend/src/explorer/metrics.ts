import type { ChannelBaselineDistributionKey } from '../types';

export type ExplorerMetricLayer = 'current' | 'baseline' | 'measured';
export type ExplorerMetricUnit = 'count' | 'duration' | 'datetime' | 'hours' | 'rate';

export type BaselineDistributionGroup =
  | 'performance'
  | 'catalogue'
  | 'publishing'
  | 'packaging';

export type BaselineDistributionDefinition = {
  id: ChannelBaselineDistributionKey;
  label: string;
  description: string;
  group: BaselineDistributionGroup;
};

export type ExplorerMetricDefinition = {
  id: string;
  label: string;
  unit: ExplorerMetricUnit;
  layers: readonly ExplorerMetricLayer[];
  missing: {
    current?: string;
    baseline?: string;
    measured?: string;
  };
  tone: 'neutral' | 'measured';
  description: string;
};

export const CHANNEL_METRICS = {
  subscribers: {
    id: 'subscribers',
    label: 'Subscribers',
    unit: 'count',
    layers: ['current'],
    missing: { current: 'Not reported' },
    tone: 'neutral',
    description: 'Current stored channel source field.',
  },
  channelViews: {
    id: 'channel-views',
    label: 'Channel views',
    unit: 'count',
    layers: ['current'],
    missing: { current: 'Not reported' },
    tone: 'neutral',
    description: 'Current stored channel-wide view count.',
  },
  publicVideos: {
    id: 'public-videos',
    label: 'Public videos',
    unit: 'count',
    layers: ['current'],
    missing: { current: 'Not reported' },
    tone: 'neutral',
    description: 'Current stored channel-wide public video count across formats.',
  },
  collectedAt: {
    id: 'collected-at',
    label: 'Channel data collected',
    unit: 'datetime',
    layers: ['current'],
    missing: { current: 'Not recorded' },
    tone: 'neutral',
    description: 'Leti Cook collection timestamp for channel source fields.',
  },
} as const satisfies Record<string, ExplorerMetricDefinition>;

export const VIDEO_METRICS = {
  views: {
    id: 'views',
    label: 'Views',
    unit: 'count',
    layers: ['current', 'baseline', 'measured'],
    missing: {
      current: 'Not reported',
      baseline: 'Not reported',
      measured: 'Not measured',
    },
    tone: 'measured',
    description: 'Stored current views or measured change between stored observations.',
  },
  likes: {
    id: 'likes',
    label: 'Likes',
    unit: 'count',
    layers: ['current', 'baseline', 'measured'],
    missing: {
      current: 'Not reported',
      baseline: 'Not reported',
      measured: 'Not measured',
    },
    tone: 'measured',
    description: 'Stored current likes or measured change between stored observations.',
  },
  comments: {
    id: 'comments',
    label: 'Comments',
    unit: 'count',
    layers: ['current', 'baseline', 'measured'],
    missing: {
      current: 'Not reported',
      baseline: 'Not reported',
      measured: 'Not measured',
    },
    tone: 'measured',
    description: 'Stored current comments or measured change between stored observations.',
  },
  duration: {
    id: 'duration',
    label: 'Duration',
    unit: 'duration',
    layers: ['current', 'baseline'],
    missing: { current: 'Not reported', baseline: 'Not reported' },
    tone: 'neutral',
    description: 'Stored video duration; unavailable in Measured change.',
  },
} as const satisfies Record<string, ExplorerMetricDefinition>;

export const EXPLORER_METRICS = {
  ...CHANNEL_METRICS,
  ...VIDEO_METRICS,
} as const;

export const BASELINE_DISTRIBUTIONS = {
  views: {
    id: 'views',
    label: 'Views distribution',
    description: 'Current stored lifetime views.',
    group: 'performance',
  },
  likes: {
    id: 'likes',
    label: 'Likes distribution',
    description: 'Current stored raw like counters.',
    group: 'performance',
  },
  comments: {
    id: 'comments',
    label: 'Comments distribution',
    description: 'Current stored raw comment counters.',
    group: 'performance',
  },
  lifetime_views_per_day: {
    id: 'lifetime_views_per_day',
    label: 'Average lifetime views/day',
    description: 'Current views divided by age at the last counter check; videos under 24 hours are excluded.',
    group: 'performance',
  },
  likes_per_1000_views: {
    id: 'likes_per_1000_views',
    label: 'Likes per 1K views',
    description: 'Current known likes per 1,000 positive known views.',
    group: 'performance',
  },
  comments_per_1000_views: {
    id: 'comments_per_1000_views',
    label: 'Comments per 1K views',
    description: 'Current known comments per 1,000 positive known views.',
    group: 'performance',
  },
  duration_seconds: {
    id: 'duration_seconds',
    label: 'Duration',
    description: 'Stored long-form video duration.',
    group: 'catalogue',
  },
  publication_years: {
    id: 'publication_years',
    label: 'Publication year',
    description: 'Stored publication dates grouped by calendar year, in local time or UTC as chosen.',
    group: 'catalogue',
  },
  publication_quarters: {
    id: 'publication_quarters',
    label: 'Publication quarter',
    description: 'Stored publication dates grouped by calendar quarter, in local time or UTC as chosen.',
    group: 'catalogue',
  },
  video_age_days: {
    id: 'video_age_days',
    label: 'Video age',
    description: 'Age at calculation time, grouped by the familiar 1, 7, 28, 90 and 365-day horizons.',
    group: 'catalogue',
  },
  categories: {
    id: 'categories',
    label: 'Official categories',
    description: 'Stored official YouTube category source field.',
    group: 'catalogue',
  },
  publication_gap_days: {
    id: 'publication_gap_days',
    label: 'Gap between publications',
    description: 'Elapsed days between consecutive publications, grouped by the familiar 1, 7, 28, 90 and 365-day horizons.',
    group: 'publishing',
  },
  publication_weekdays_utc: {
    id: 'publication_weekdays_utc',
    label: 'Day of week',
    description: 'Stored publication timestamps grouped by weekday, in local time or UTC as chosen.',
    group: 'publishing',
  },
  publication_hours_utc: {
    id: 'publication_hours_utc',
    label: 'Publication hour',
    description: 'Stored publication timestamps grouped into six-hour windows, in local time or UTC as chosen.',
    group: 'publishing',
  },
  title_length_characters: {
    id: 'title_length_characters',
    label: 'Title length',
    description: 'Stored title length measured in Unicode characters.',
    group: 'packaging',
  },
  tag_count: {
    id: 'tag_count',
    label: 'Tag count',
    description: 'Number of stored tags; missing tag data remains unknown.',
    group: 'packaging',
  },
} as const satisfies Record<ChannelBaselineDistributionKey, BaselineDistributionDefinition>;
