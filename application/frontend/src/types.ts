import type { CollectionExplanation } from './collectionExplanation';
export type Primitive = string | number | boolean | null | undefined;
export type VideoType = 'long' | 'short' | 'stream';

export type Channel = {
  channel_id?: string;
  /** Original tracking date, preserved by backups; not the YouTube creation date. */
  added_at?: string | null;
  title?: string | null;
  avatar_url?: string | null;
  subscribers?: Primitive;
  views?: Primitive;
  video_count?: Primitive;
  long_video_count?: Primitive;
  country_code?: string | null;
  country_collected_at?: string | null;
  channel_created_at?: string | null;
  folder_id?: string | null;
  parent_channel_id?: string | null;
  initial_long_inventory_status?: string | null;
  initial_28d_stats_status?: string | null;
  deep_inventory_status?: string | null;
  channel_metadata_status?: string | null;
  channel_discovery_status?: string | null;
  channel_metadata_checked_at?: string | null;
  channel_discovery_checked_at?: string | null;
  /** Channels YouTube no longer returns: when their stored YouTube data is removed; null when already expired. */
  youtube_data_expires_at?: string | null;
  /** Channels YouTube no longer returns: the day a check first found it missing; null when unknown. */
  channel_metadata_not_found_since?: string | null;
  label_keys?: string[];
};

export type ExplorerLabel = {
  label_key: string;
  name: string;
  label_type: 'preset' | 'custom';
};

export type Folder = {
  id?: string;
  name?: string;
};

// Creating a name that already exists returns that item with created: false.
export type CreatedFolder = Folder & { created?: boolean };
export type CreatedLabel = ExplorerLabel & { created?: boolean };



export type ChannelExplorerVideo = {
  video_id?: string;
  video_type?: 'long' | 'short' | 'stream';
  title?: string;
  thumbnail_url?: string | null;
  published_at?: string;
  checked_at?: string;
  duration?: Primitive;
  views?: Primitive;
  like_count?: Primitive;
  comment_count?: Primitive;
  category_id?: string | null;
  category_name?: string | null;
  tags?: string[] | null;
};

export type ChannelVideoViewGrowthStatus =
  | 'ready'
  | 'partial'
  | 'started_today'
  | 'waiting_next_snapshot'
  | 'unavailable';

export type ChannelVideoViewGrowth = {
  status: ChannelVideoViewGrowthStatus;
  delta_views: number | string | null;
  tracked_days: number | null;
  observed_start_at: string | null;
  observed_end_at: string | null;
  actual_interval_seconds: number | null;
  /** Optional additive v1 field; absent in older frozen catalogue generations. */
  average_views_per_day?: string | null;
  boundary_distance_seconds: number | null;
  reason: string | null;
};

export type VideoHistoryInterval = {
  from_at: string;
  to_at: string;
  actual_interval_seconds: string;
  delta_views: string | null;
  views_per_day: string | null;
};

export type VideoInteractionHistoryInterval = {
  from_at: string;
  to_at: string;
  actual_interval_seconds: string;
  delta: string | null;
  per_day: string | null;
};

export type VideoInteractionHistorySeries = {
  status: 'ready' | 'unavailable';
  reason: null | 'endpoint_snapshot_missing' | 'endpoint_counter_mismatch' | 'invalid_catalogue_endpoint' | 'catalogue_endpoint_outside_hot_window' | 'conflicting_simultaneous_observations';
  points: { recorded_at: string; value: string | null }[];
  intervals: VideoInteractionHistoryInterval[];
  latest_interval: VideoInteractionHistoryInterval | null;
  coverage: { point_count: number; valid_count: number; missing_count: number; duplicate_timestamp_count: number };
};

export type VideoLaunchComparison = {
  status: 'unavailable' | 'insufficient_data' | 'preliminary' | 'ready';
  reference_count: number;
  median: string | null;
  p25: string | null;
  p75: string | null;
  classification: 'below_typical' | 'typical' | 'above_typical' | null;
  multiplier: string | null;
  multiplier_reason: 'zero_reference_median' | null;
};

export type VideoLaunchHorizon = {
  key: '24h' | '7d' | '28d';
  horizon_seconds: number;
  tolerance_before_seconds: number;
  eligible_count: number;
  quality: 'insufficient_data' | 'preliminary' | 'ready';
  counts: { below_typical: number; typical: number; above_typical: number };
  target: {
    status: 'ready' | 'not_reached' | 'missing_publication' | 'outside_observation_window' | 'missing_snapshot'
      | 'invalid_snapshot' | 'conflicting_snapshots' | 'ineligible_video';
    target_at: string | null;
    observed_at: string | null;
    actual_age_seconds: string | null;
    views: string | null;
    comparison: VideoLaunchComparison;
  };
};

export type ChannelExplorerCategory = {
  category_id: string;
  category_name: string;
};

export type ChannelExplorerChannel = {
  channel_id?: string;
  title?: string;
  subscribers?: Primitive;
  views?: Primitive;
  video_count?: Primitive;
  country_code?: string | null;
  channel_created_at?: string | null;
  channel_metadata_checked_at?: string | null;
};

export type ChannelExplorerProfilePayload = {
  contract_version: number;
  generated_at: string;
  channel: ChannelExplorerChannel;
  public_video_count_notice?: {
    code: 'public_video_count_decreased';
    checked_at: string;
    source_expires_at: string;
  } | null;
  freshness: {
    maximum_age_days: number;
    channel_data_fresh: boolean;
  };
  source: {
    kind: 'stored_channel_profile';
    youtube_requests: false;
    database_writes: false;
  };
};

export type ChannelExplorerFormatStatus =
  | 'ready'
  | 'incomplete'
  | 'stale'
  | 'not_collected'
  | 'absent'
  /** A format turned off in config.ini. */
  | 'turned_off';

export type ChannelExplorerListInventory = {
  complete: boolean;
  unique_video_count: number | null;
  youtube_video_count: number | null;
  source_expires_at: string | null;
  formats: Record<'long' | 'short' | 'stream', {
    status: 'complete' | 'absent' | 'not_collected' | 'incomplete' | 'stale';
    listed_video_count: number | null;
    source_expires_at: string | null;
  }>;
};

export type ManualListCheck = {
  epoch: number | null;
  unavailable: string | null;
  estimated_general: number | null;
  quota_retry_at: string | null;
  request: {
    id: string; epoch: number; state: 'queued' | 'running' | 'complete' | 'absent' | 'failed';
    pages: number; general: number; wait: string | null; retry_at?: string | null;
  } | null;
};

/** GET /channels/{id}/explorer/list-inventory: the list proof of the unlisted note. */
export type ChannelExplorerListInventoryPayload = {
  contract_version: number;
  channel_id: string;
  generated_at: string;
  inventory: ChannelExplorerListInventory;
  library?: {
    status: 'ready' | 'partial';
    formats: Record<VideoType, {
      status: 'complete' | 'absent' | 'checking' | 'check_failed' | 'waiting_for_quota' | 'partial' | 'not_checked' | 'turned_off';
      manual_check?: ManualListCheck;
      full_completed_at: string | null;
      absent_checked_at: string | null;
      source_expires_at: string | null;
    }>;
  };
  source: {
    kind: 'stored_channel_list_inventory';
    youtube_requests: false;
    database_writes: false;
  };
};

export type ChannelExplorerCounterDynamicsStatus =
  | 'ready'
  | 'partial'
  | 'not_reported'
  | 'not_enough_history'
  | 'boundary_too_distant'
  | 'stale_current'
  | 'zero_baseline'
  | 'unavailable';

export type ChannelExplorerCounterDynamicsValue = number | string | null;

export type ChannelExplorerCounterDynamicsMetric = {
  status: ChannelExplorerCounterDynamicsStatus;
  coverage?: 'full' | 'partial' | 'unavailable';
  start_value: ChannelExplorerCounterDynamicsValue;
  end_value: ChannelExplorerCounterDynamicsValue;
  absolute_change: ChannelExplorerCounterDynamicsValue;
  percent_change: ChannelExplorerCounterDynamicsValue;
  observed_start_at: string | null;
  observed_end_at: string | null;
  actual_interval_seconds: ChannelExplorerCounterDynamicsValue;
  reason: string | null;
};

export type ChannelExplorerCounterDynamicsPeriodKey = '7d' | '28d';

export type ChannelExplorerCounterDynamicsPeriod = {
  period: ChannelExplorerCounterDynamicsPeriodKey;
  window_seconds: number;
  metrics: {
    subscribers: ChannelExplorerCounterDynamicsMetric;
    views: ChannelExplorerCounterDynamicsMetric;
  };
};

export type ChannelExplorerCounterDynamicsPeriodsPayload = {
  contract_version: number;
  channel_id: string;
  generated_at: string;
  periods: Record<
    ChannelExplorerCounterDynamicsPeriodKey,
    ChannelExplorerCounterDynamicsPeriod
  >;
  methods: {
    period_semantics: 'actual_intervals_within_rolling_windows';
    boundary: 'earliest_known_metric_at_or_after_window_start';
    boundary_tolerance_seconds: number;
    current: 'latest_stored_channel_snapshot_at_or_before_generated_at';
    current_max_age_seconds: number;
    percent_change: '(end_value-start_value)/start_value*100';
    counter_corrections: 'preserved_as_signed_changes';
    interpolation: 'none';
    timezone: 'UTC';
  };
  source: {
    kind: 'derived_from_stored_channel_snapshots';
    youtube_requests: false;
    database_writes: false;
    snapshots_used: true;
  };
};

export type ChannelBaselinePeriodKey = '7d' | '28d' | '3m' | '1y' | 'max';

export type ChannelBaselineMetricSummary = {
  known_count?: Primitive;
  missing_count?: Primitive;
  total?: Primitive;
  mean?: Primitive;
  median?: Primitive;
  q1?: Primitive;
  q3?: Primitive;
  min?: Primitive;
  max?: Primitive;
};

export type ChannelBaselineBucket = {
  key?: string;
  label?: string;
  count?: Primitive;
};

export type ChannelBaselineDistributionCoverage = {
  known_count?: Primitive;
  missing_count?: Primitive;
  excluded_count?: Primitive;
};

export type ChannelBaselinePublicationCalendarDay = {
  date: string;
  count: number;
};

export type ChannelBaselinePublicationCalendarYear = {
  year: number;
  days: ChannelBaselinePublicationCalendarDay[];
};

export type ChannelBaselinePublicationCalendar = {
  timezone: string;
  default_year: number;
  available_years: number[];
  years: ChannelBaselinePublicationCalendarYear[];
};

export type ChannelBaselineBreakdownSource =
  | 'youtube.video.snippet.categoryId'
  | 'youtube.video.topicDetails.topicCategories'
  | 'youtube.video.snippet.defaultAudioLanguage'
  | 'youtube.video.snippet.defaultLanguage'
  | 'youtube.video.contentDetails.caption'
  | 'leti_cook.video_age_from_published_at';

export type ChannelBaselineBreakdownGroup = {
  key: string;
  label: string;
  count: number;
  share_percent: Primitive;
  /** Optional medians for the baseline group. */
  median_views?: Primitive;
  median_likes?: Primitive;
  median_comments?: Primitive;
  views_known_count?: number;
  likes_known_count?: number;
  comments_known_count?: number;
};

export type ChannelBaselineBreakdownSection = {
  kind: 'single_label' | 'multi_label';
  source: ChannelBaselineBreakdownSource;
  /** Topics share the videos with collected topics. */
  share_basis: 'known_rows' | 'sample_rows' | 'collected_rows';
  sample_count: number;
  known_count: number;
  missing_count: number;
  assigned_count?: number;
  empty_count?: number;
  groups: ChannelBaselineBreakdownGroup[];
  /** Groups beyond the listed ones (the shared operation lists at most 250 per section). */
  omitted_group_count?: number;
};

export type ChannelBaselineContentBreakdown = {
  official_categories: ChannelBaselineBreakdownSection;
  youtube_topics: ChannelBaselineBreakdownSection;
  audio_languages: ChannelBaselineBreakdownSection;
  metadata_languages: ChannelBaselineBreakdownSection;
  captions: ChannelBaselineBreakdownSection;
  catalogue_age: ChannelBaselineBreakdownSection;
};

export type SidebarData = {
  channels?: Channel[];
  folders?: Folder[];
  labels?: ExplorerLabel[];
};

export type ExplorerChannelLookupResult = {
  contract_version: 1;
  status: 'matched' | 'not_tracked';
  video_id: string;
  channel_id: string | null;
  source: {
    kind: 'stored_video_channel_mapping';
    youtube_requests: false;
    database_writes: false;
  };
};

export type SyncRun = {
  explanation?: CollectionExplanation;
  id?: string;
  status?: string;
  run_type?: string;
  started_at?: string;
  finished_at?: string;
  current_phase?: string;
  phase_state?: string;
  progress_updated_at?: string;
  duration_seconds?: Primitive;
  errors?: Primitive;
  metadata_processed?: Primitive;
  metadata_updated?: Primitive;
  video_count_changed?: Primitive;
  discovery_processed?: Primitive;
  new_videos?: Primitive;
  new_video_stats_updated?: Primitive;
  regular_stats_requested?: Primitive;
  regular_stats_updated?: Primitive;
  stats_updated?: Primitive;
  unavailable_marked?: Primitive;
  missing?: Primitive;
  summary_json?: unknown;
};

export type QueueHealth = PipelineHealthQueue & { collection_capacity?: { status: string; stats_limit: number | null } };

export type SystemWarning = {
  code?: string;
  message?: string;
  count?: Primitive;
};


export type QuotaSummary = {
  used?: number | null;
  units_used?: number | null;
  limit?: number;
  remaining?: number | null;
  read_status?: 'ok' | 'partial' | 'unavailable';
  ledger_attempts?: number | null;
  reset_at?: string;
  warnings?: SystemWarning[];
};

export type SystemSummary = {
  version?: string;
  status?: string;
  status_reason?: string;
  warnings?: SystemWarning[];
  provider_key?: { status?: string } | null;
  collection?: { state?: string } | null;
  latest_run?: SyncRun | null;
  recent_runs?: SyncRun[];
  recent_runs_read_status?: 'ok' | 'unavailable';
  queue_health?: QueueHealth | null;
  video_type_reviews?: { available?: boolean };
  quota?: QuotaSummary;
  next_auto_sync?: Record<string, Primitive> | null;
  now?: string;
};

export type PipelineHealthDurationStats = {
  count?: Primitive;
  min?: Primitive;
  avg?: Primitive;
  median?: Primitive;
  max?: Primitive;
};

export type PipelineHealthRunKind = 'running' | 'complete' | 'carried_over' | 'quota' | 'rate_limited' | 'failed';

export type PipelineHealthSync = SyncRun & {
  age_minutes?: Primitive;
  run_kind?: PipelineHealthRunKind;
  regular_video_stats?: Record<string, unknown>;
  channel_discovery?: Record<string, unknown>;
};

export type PipelineHealthQueue = {
  scope?: string;
  unit?: string;
  by_type?: Partial<Record<VideoType | 'unknown', PipelineHealthQueue>>;
  /** This type bucket belongs to a format turned off in config.ini. */
  turned_off?: boolean;
  /** Formats turned off in config.ini; their work is left out of the totals. */
  turned_off_types?: VideoType[];
  total_channels?: Primitive;
  excluded_failed?: Primitive;
  runtime_disabled?: Primitive;
  playlist_disabled?: Primitive;
  initial_owned?: Primitive;
  playlist_not_found?: number;
  policy_paused?: number;
  mode?: string;
  total?: Primitive;
  leased?: Primitive;
  expired_leases?: Primitive;
  paused?: Primitive;
  hydration_due?: Primitive;
  metadata_due?: Primitive;
  counter_due?: Primitive;
  failed?: Primitive;
  ready?: Primitive;
  reconciliation_required?: boolean;
  oldest_overdue_at?: string | null;
  due?: Primitive;
  null_schedule?: Primitive;
  running?: Primitive;
  oldest_overdue_minutes?: Primitive;
  oldest_running_minutes?: Primitive;
  status_counts?: Record<string, number>;
  active_resume_states?: Primitive;
  oldest_resume_age_minutes?: Primitive;
  streak_buckets?: Record<string, number>;
  all_due?: Primitive;
  fresh_due_under_7d?: Primitive;
  due_under_28d?: Primitive;
  older_due_28d_plus?: Primitive;
  missing_stats_from_inventory?: Primitive;
  unavailable?: Primitive;
  due_rows_scanned?: Primitive;
  scan_limit?: Primitive;
  scan_truncated?: boolean;
  never_checked_in_scan?: Primitive;
  oldest_checked_age_minutes_in_scan?: Primitive;
};

export type PipelineHealthQuota = {
  read_status?: 'ok' | 'partial' | 'unavailable';
  quota_day?: string;
  timezone?: string;
  next_reset_at?: string;
  minutes_until_reset?: Primitive;
  attempts?: Primitive;
  quota_units?: Primitive;
  bucket_attempt_counts?: Record<string, number>;
  bucket_units?: Record<string, number>;
  phase_attempt_counts?: Record<string, number>;
  phase_units?: Record<string, number>;
  endpoint_attempt_counts?: Record<string, number>;
  status_counts?: Record<string, number>;
  http_status_counts?: Record<string, number>;
  error_type_counts?: Record<string, number>;
  duration_ms?: PipelineHealthDurationStats;
  unfinished?: {
    count?: Primitive;
    stale_count?: Primitive;
    oldest_age_minutes?: Primitive;
    stale_after_minutes?: Primitive;
  };
  configured_bucket_limits?: Record<string, number>;
  estimated_bucket_remaining?: Record<string, number | null>;
  /** YouTube refused this bucket with quotaExceeded today; it resumes at the reset. */
  exhausted_buckets?: Record<string, boolean>;
  remaining_is_ledger_estimate?: boolean;
  remaining_is_google_console_authoritative?: boolean;
  truncated?: boolean;
};

export type PipelineHealthReport = {
  history?: {
    sync_errors: number | null;
    transport_errors: number | null;
    transport_read_status: 'ok' | 'partial' | 'unavailable';
    quota_day: string;
    recovery: 'not_correlated';
  };
  status?: 'ok' | 'warning' | 'critical' | string;
  generated_at?: string;
  provider_key?: { status?: string } | null;
  collection?: { state?: string } | null;
  window_hours?: Primitive;
  runtime?: {
    batch_stats_enabled?: boolean;
    database_video_collection_enabled?: boolean;
    database_channel_discovery_enabled?: boolean;
  };
  latest_sync?: PipelineHealthSync | null;
  // The last full pass; the last sync without errors may be partial.
  last_successful_sync?: PipelineHealthSync | null;
  last_sync_without_errors?: PipelineHealthSync | null;
  server_started_at?: string | null;
  collection_gap?: {
    server_started_at: string;
    collection_last_ran_at: string;
    hours: number;
  } | null;
  recent_syncs?: {
    count?: Primitive;
    status_counts?: Record<string, number>;
    error_total?: Primitive;
    duration_seconds?: PipelineHealthDurationStats;
  };
  queues?: {
    stats?: PipelineHealthQueue;
    channel_discovery?: PipelineHealthQueue;
    channel_metadata?: PipelineHealthQueue;
  };
  channels?: {
    total?: Primitive;
    visible?: Primitive;
  };
  youtube_quota?: PipelineHealthQuota;
  critical?: string[];
  warnings?: string[];
  collection_errors?: Array<{
    component?: string;
    error_type?: string;
  }>;
};

export type TelemetryEvent = {
  id?: string;
  ts?: string;
  timestamp?: string;
  kind?: string;
  operation?: string;
  event?: string;
  status?: string;
  phase?: string;
  direction?: string;
  source?: string;
  target?: string;
  detail?: string;
  count?: Primitive;
  quota?: Primitive;
  duration_ms?: Primitive;
  [key: string]: unknown;
};
