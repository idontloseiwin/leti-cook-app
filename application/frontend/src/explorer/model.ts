import type {
  Channel,
  ChannelBaselinePayload,
  ChannelVideoGrowthPayload,
  ChannelExplorerPayload,
} from '../types';

export type ExplorerMedia = 'videos' | 'shorts' | 'streams';
export type ExplorerSection = 'overview' | 'measured-change' | 'videos' | 'data-quality';
export type CapabilityAvailability = 'available' | 'unavailable';

export type ExplorerCapabilities = {
  contract_version: 1;
  channel_explorer: {
    raw: CapabilityAvailability;
    baseline: CapabilityAvailability;
    measured_change: CapabilityAvailability;
    timeline: CapabilityAvailability;
    video_growth: CapabilityAvailability;
    filter_facets?: CapabilityAvailability;
    static_video_details?: CapabilityAvailability;
    video_history?: CapabilityAvailability;
    video_launch?: CapabilityAvailability;
  };
};

export type SavedVideoCounts = {
  total: number;
  long: number;
  short: number;
  stream: number;
};

export type ExplorerHomeSummary = {
  contract_version: 1;
  last_successful_sync_at: string | null;
  last_data_change_at: string | null;
  saved_videos: SavedVideoCounts | null;
  source: {
    kind: 'stored_typed_video_inventory';
    format_source: 'video_inventory.video_type';
    scope: 'visible_channels';
    youtube_requests: false;
    database_writes: false;
  };
};

export type ExplorerRecentWindow = '24h' | '7d' | '28d';

export type ExplorerRecentVideo = {
  video_id: string;
  channel_id: string;
  title: string | null;
  thumbnail_url: string | null;
  published_at: string;
  checked_at: string | null;
  views: number | string | null;
  like_count: number | string | null;
  comment_count: number | string | null;
};

export type ExplorerRecentVideosPage = {
  contract_version: 1;
  window: ExplorerRecentWindow;
  generated_at: string;
  published_from: string;
  items: ExplorerRecentVideo[];
  has_more: boolean;
  next_cursor: string | null;
  source: {
    kind: 'stored_recent_long_form_videos';
    format_source: 'video_inventory.video_type';
    scope: 'visible_channels';
    youtube_requests: false;
    database_writes: false;
  };
};

export type ExplorerHydrationStatus =
  | 'idle'
  | 'confirmation_required'
  | 'queued'
  | 'running'
  | 'ready'
  | 'completed'
  | 'blocked'
  | 'error'
  | 'suspended';

export type ExplorerHydrationPhase =
  | 'inventory'
  | 'hydrate_videos'
  | 'verify'
  | 'ready';

export type ExplorerHydrationAction =
  | 'confirm'
  | 'start'
  | 'retry';

export type ExplorerHydrationProgress = {
  processed: number;
  total: number | null;
  percent: number | null;
  discovered?: number; // Required by hydration contract 2.
};

export type ExplorerHydrationEstimate = {
  items: number | null;
  pages: number | null;
  quota_units: number | null;
  quota_basis?: 'before_start';
  // Legacy contract 1 may omit these; contract 2 always supplies them.
  estimate_kind?: 'upper_bound' | 'unknown' | 'exact_workset';
  items_lower_bound?: number;
  items_upper_bound?: number | null;
};

export type ExplorerHydrationPayload = {
  contract_version: 1 | 2;
  channel_id: string;
  status: ExplorerHydrationStatus;
  phase: ExplorerHydrationPhase | null;
  current_profile_ready: boolean;
  inventory_ready: boolean;
  all_initial_checks_complete?: boolean;
  progress: ExplorerHydrationProgress;
  estimate: ExplorerHydrationEstimate | null;
  confirmation_required: boolean;
  action_required: ExplorerHydrationAction | null;
  job: Record<string, unknown> | null;
};

export type ExplorerAddChannelResult = {
  status?: 'added' | 'restored' | 'exists' | string;
  title?: string;
  channel?: Channel;
  hydration?: ExplorerHydrationPayload;
  error?: string;
  detail?: string;
  code?: string;
};

export type ExplorerHydrationState =
  | { kind: 'loading' }
  | { kind: 'ready'; payload: ExplorerHydrationPayload }
  | { kind: 'confirming'; payload: ExplorerHydrationPayload }
  | { kind: 'error'; message: string; previous?: ExplorerHydrationPayload };

export type CapabilityState =
  | { kind: 'loading' }
  | { kind: 'ready'; payload: ExplorerCapabilities }
  | { kind: 'error'; message: string };

export type ExplorerFilters = {
  q: string;
  category_id: string;
  published_from: string;
  published_before: string;
  min_views: string;
  max_views: string;
  min_duration_seconds: string;
  max_duration_seconds: string;
  min_likes?: string;
  max_likes?: string;
  min_comments?: string;
  max_comments?: string;
  min_title_length?: string;
  max_title_length?: string;
  min_tag_count?: string;
  max_tag_count?: string;
  min_like_rate?: string;
  max_like_rate?: string;
  min_comment_rate?: string;
  max_comment_rate?: string;
  sort_by: string;
  sort_order: 'asc' | 'desc';
};

export type ExplorerFilterKey = keyof ExplorerFilters;
export type ExplorerFilterErrors = Partial<Record<ExplorerFilterKey, string>>;

export type ResourceState<T> =
  | { kind: 'idle' }
  | { kind: 'loading'; scopeKey: string }
  | { kind: 'ready'; scopeKey: string; payload: T }
  | { kind: 'updating'; scopeKey: string; payload: T }
  | { kind: 'error'; scopeKey: string; message: string; previous?: T }
  | { kind: 'incompatible'; scopeKey: string; message: string; previous?: T };

export type VideoCatalogueState = ResourceState<ChannelExplorerPayload | ChannelVideoGrowthPayload>;
export type BaselineState = ResourceState<ChannelBaselinePayload>;

export type DataQualityIssue = {
  id: string;
  tone: 'neutral' | 'warning' | 'error';
  message: string;
};
