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

export type ChannelVideoGrowthVideo = ChannelExplorerVideo & {
  view_growth: ChannelVideoViewGrowth;
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

export type VideoHistoryPayload = {
  contract_version: 1;
  channel_id: string;
  video_id: string;
  browse_session: string;
  generated_at: string;
  browse_created_at: string;
  expires_at: string;
  status: 'ready' | 'unavailable';
  reason: null | 'endpoint_snapshot_missing' | 'endpoint_counter_mismatch' | 'invalid_catalogue_endpoint' | 'catalogue_endpoint_outside_hot_window';
  window: { requested_days: 28; from_at: string | null; to_at: string | null; endpoint: 'frozen_catalogue_checked_at' };
  points: { recorded_at: string; views: string | null }[];
  intervals: VideoHistoryInterval[];
  latest_interval: VideoHistoryInterval | null;
  /** Optional additive v1 series; older servers remain Views-only. */
  interaction_metrics?: {
    window: { requested_days: 28; from_at: string | null; to_at: string | null; endpoint: 'frozen_catalogue_checked_at' };
    scanned_count: number;
    likes: VideoInteractionHistorySeries;
    comments: VideoInteractionHistorySeries;
  };
  coverage: { complete: true; scanned_count: number; point_count: number; valid_view_count: number; missing_view_count: number; duplicate_timestamp_count: number; row_limit: 500 };
  methods: {
    ordering: 'recorded_at_ascending';
    same_timestamp: 'identical_views_collapsed_conflicts_rejected';
    gaps: 'no_interpolation_or_daily_allocation';
    rate: 'signed_delta_per_exact_elapsed_second_times_86400_rounded_2dp_half_even';
    window: 'strict_28d_not_card_comparison_interval';
    interaction_metrics?: 'independent_stored_counters_same_observation_window';
  };
  source: { kind: 'derived_video_snapshot_history'; snapshots_used: true; youtube_requests: false; database_writes: false; attribution: 'Leti Cook' };
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

export type VideoLaunchPayload = {
  contract_version: 1;
  channel_id: string;
  video_id: string;
  browse_session: string;
  generated_at: string;
  browse_created_at: string;
  expires_at: string;
  method: 'measured_launch_before_horizon_v1';
  window: { days: 28; from_at: string; to_at: string };
  reference: { video_type: 'long'; publication_period: 'all_time'; browsing_filters_applied: false; leave_one_out: true };
  coverage: { inventory_count: number; eligible_video_count: number; excluded_video_count: number;
    snapshot_rows: number; snapshot_row_limit: 20000; complete: true };
  horizons: VideoLaunchHorizon[];
  source: { snapshots_used: true; youtube_requests: false; database_writes: false; attribution: 'Leti Cook' };
};

export type VideoLifetimeBaselinePayload = {
  contract_version: 1;
  channel_id: string;
  video_id: string;
  browse_session: string;
  generated_at: string;
  expires_at: string;
  method: 'current_lifetime_views_median_iqr_v1';
  status: 'insufficient_data' | 'preliminary' | 'ready';
  sample_size: number;
  missing_views: number;
  median: number | string | null;
  p25: number | string | null;
  p75: number | string | null;
  coverage: { inventory_count: number; eligible_count: number; excluded_count: number };
  reference: { video_type: 'long'; publication_period: 'all_time'; browsing_filters_applied: false };
};

export type VideoStaticDetailsPayload = {
  contract_version: 1;
  channel_id: string;
  video_id: string;
  browse_session: string;
  generated_at: string;
  expires_at: string;
  metrics: { like_rate_percent: number | null; comment_rate_percent: number | null; title_length_characters: number | null; tag_count: number | null };
  raw: { tags: string[] | null; metadata_checked_at: string | null };
  method: { rates: 'counter / views * 100'; zero_views: 'unavailable'; title_length: 'unicode_code_points'; tag_count: 'stored_array_length' };
  source: { kind: 'derived_video_static_details'; attribution: 'Leti Cook'; snapshots_used: false; youtube_requests: false; database_writes: false };
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
  | 'absent';

export type ChannelExplorerFormatTotal = {
  status: ChannelExplorerFormatStatus;
  views: Primitive;
  scanned_inventory_count?: number;
  known_view_count?: number;
  missing_view_count?: number;
  reason?: 'inventory_not_completed' | 'inventory_stale' | 'inventory_sources_incomplete' | 'playlist_not_found';
  coverage?: {
    scan_limit: number;
    inventory_complete: boolean;
    inventory_ready: boolean;
    product_exposure_ready: boolean;
    missing_video_row_count: number;
    unknown_view_count: number;
    stale_counter_count: number;
    stale_inventory_count?: number;
    availability_withheld_count?: number;
    unavailable_excluded_count?: number;
    stale_metadata_count: number;
    type_resolution_withheld: number;
    stream_completion_withheld: number;
    transactional_snapshot: false;
  };
};

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

export type ChannelExplorerFormatTotalsPayload = {
  inventory?: ChannelExplorerListInventory;
  contract_version: number;
  channel_id: string;
  generated_at: string;
  formats: {
    long: ChannelExplorerFormatTotal;
    short: ChannelExplorerFormatTotal;
    stream: ChannelExplorerFormatTotal;
  };
  coverage: {
    scan_limit: number;
    inventory_complete: boolean;
    missing_video_row_count: number;
    unknown_view_count: number;
    stale_counter_count: number;
    stale_inventory_count?: number;
    availability_withheld_count?: number;
    unavailable_excluded_count?: number;
    transactional_snapshot: false;
  };
  source: {
    kind: 'derived_from_stored_typed_video_inventory';
    youtube_requests: false;
    database_writes: false;
    snapshots_used: false;
    freshness_enforced: true;
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

export type ChannelExplorerPayload = {
  contract_version: number;
  video_type?: VideoType;
  channel?: ChannelExplorerChannel;
  filters?: {
    q?: string;
    category_id?: string;
    published_from?: string | null;
    published_before?: string | null;
    min_views?: Primitive;
    max_views?: Primitive;
    min_duration_seconds?: Primitive;
    max_duration_seconds?: Primitive;
    sort_by?: string;
    sort_order?: string;
    limit?: Primitive;
    offset?: Primitive;
  };
  coverage?: {
    scope?: string;
    video_type?: VideoType;
    scan_limit?: Primitive;
    scanned_count?: Primitive;
    inventory_status?: string;
    inventory_ready?: boolean;
    product_exposure_ready?: boolean;
    inventory_complete?: boolean;
    inventory_source_checked_at?: string | null;
    type_resolution_withheld?: Primitive;
    loaded_video_count?: Primitive;
    missing_video_row_count?: Primitive;
    inventory_pagination?: string;
    transactional_snapshot?: boolean;
    complete?: boolean;
    truncated?: boolean;
    category_cache_available?: boolean;
    category_cache_fresh?: boolean;
    category_cache_refreshed_at?: string | null;
    current_data_max_age_days?: Primitive;
    channel_data_fresh?: boolean;
    video_rows_withheld_stale?: Primitive;
    video_rows_withheld_without_stream_completion_evidence?: Primitive;
    video_rows_with_stale_counters?: Primitive;
    video_rows_with_stale_metadata?: Primitive;
  };
  pagination?: {
    matched_count?: number;
    limit?: Primitive;
    offset?: Primitive;
    returned_count?: Primitive;
    has_more_in_scan?: boolean;
  };
  options?: {
    categories?: ChannelExplorerCategory[];
    sort_fields?: string[];
  };
  videos?: ChannelExplorerVideo[];
  source?: {
    kind?: string;
    youtube_requests?: boolean;
    database_writes?: boolean;
    snapshots_used?: boolean;
    derived_performance_metrics?: boolean;
    freshness_enforced?: boolean;
  };
};

export type ChannelVideoGrowthCoverage = NonNullable<ChannelExplorerPayload['coverage']> & {
  growth: {
    requested_video_count: number;
    returned_growth_row_count: number;
    calculable_count: number;
    ready_count: number;
    partial_count: number;
    started_today_count: number;
    waiting_next_snapshot_count: number;
    unavailable_count: number;
  };
};

export type ChannelVideoGrowthPayload = {
  contract_version: number;
  generated_at: string;
  channel: NonNullable<ChannelExplorerPayload['channel']>;
  filters: NonNullable<ChannelExplorerPayload['filters']>;
  coverage: ChannelVideoGrowthCoverage;
  pagination: NonNullable<ChannelExplorerPayload['pagination']>;
  options: NonNullable<ChannelExplorerPayload['options']>;
  videos: ChannelVideoGrowthVideo[];
  window: {
    metric: 'views';
    requested_days: 28;
    boundary_tolerance_days: 0;
    endpoint: 'per_video_current_checked_at';
  };
  methods: {
    endpoint_observation: 'current_video_row_with_matching_snapshot';
    full_window_start: 'exact_28d_interval_inside_hot_window';
    partial_window_start: 'earliest_snapshot_inside_hot_window';
    partial_minimum_seconds: 86400;
    average_views_per_day?: 'signed_delta_per_actual_second_times_86400_rounded_2dp_half_even';
    counter_decrease: 'reported_as_correction_not_clamped';
    sorting: 'signed_delta_ready_and_partial_nulls_last_then_video_id';
    filters_applied_before_growth_read: boolean;
    growth_filters_applied_after_growth_read?: true;
    read_scope?: 'full_eligible_catalogue_for_contextual_facets';
    pagination_applied_after_sorting: true;
  };
  source: {
    kind: 'derived_video_view_growth_from_stored_snapshots';
    format_source: 'video_inventory.video_type';
    youtube_requests: false;
    database_writes: false;
    snapshots_used: true;
    availability_history_used: false;
    derived_performance_metrics: true;
    forecasts_used: false;
    performance_ranks_used: false;
    freshness_enforced: true;
  };
};

export type ChannelExplorerQuery = {
  full_catalogue?: boolean;
  browse_session?: string;
  catalogue_filters?: string;
  q?: string;
  category_id?: string;
  published_from?: string;
  published_before?: string;
  min_views?: string;
  max_views?: string;
  min_duration_seconds?: string;
  max_duration_seconds?: string;
  sort_by?: string;
  sort_order?: 'asc' | 'desc';
  limit?: number;
  offset?: number;
};

export type ChannelBaselinePeriodKey = '7d' | '28d' | '3m' | '1y' | 'max';

export type ChannelBaselineQuery = Pick<
  ChannelExplorerQuery,
  | 'q'
  | 'category_id'
  | 'published_from'
  | 'published_before'
  | 'min_views'
  | 'max_views'
  | 'min_duration_seconds'
  | 'max_duration_seconds'
> & {
  period?: ChannelBaselinePeriodKey;
  full_catalogue?: boolean;
  catalogue_filters?: string;
};

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

export type ChannelBaselineCategory = {
  category_id?: string;
  category_name?: string | null;
  count?: Primitive;
  share_percent?: Primitive;
};

export type ChannelBaselineDistributionKey =
  | 'views'
  | 'likes'
  | 'comments'
  | 'lifetime_views_per_day'
  | 'likes_per_1000_views'
  | 'comments_per_1000_views'
  | 'duration_seconds'
  | 'publication_years'
  | 'publication_quarters'
  | 'video_age_days'
  | 'categories'
  | 'publication_gap_days'
  | 'publication_weekdays_utc'
  | 'publication_hours_utc'
  | 'title_length_characters'
  | 'tag_count';

export type ChannelBaselineDistributionCoverage = {
  known_count?: Primitive;
  missing_count?: Primitive;
  excluded_count?: Primitive;
};

export type ChannelBaselinePublicationPoint = {
  video_id: string;
  title: string | null;
  published_at: string;
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
  median_views: Primitive;
  median_likes: Primitive;
  median_comments: Primitive;
  views_known_count: number;
  likes_known_count: number;
  comments_known_count: number;
};

export type ChannelBaselineBreakdownSection = {
  kind: 'single_label' | 'multi_label';
  source: ChannelBaselineBreakdownSource;
  share_basis: 'known_rows' | 'sample_rows';
  sample_count: number;
  known_count: number;
  missing_count: number;
  assigned_count?: number;
  empty_count?: number;
  groups: ChannelBaselineBreakdownGroup[];
};

export type ChannelBaselineContentBreakdown = {
  official_categories: ChannelBaselineBreakdownSection;
  youtube_topics: ChannelBaselineBreakdownSection;
  audio_languages: ChannelBaselineBreakdownSection;
  metadata_languages: ChannelBaselineBreakdownSection;
  captions: ChannelBaselineBreakdownSection;
  catalogue_age: ChannelBaselineBreakdownSection;
};

export type ChannelBaselinePayload = {
  contract_version: number;
  video_type?: VideoType;
  format?: 'long' | string;
  generated_at?: string;
  period?: {
    key: ChannelBaselinePeriodKey;
    label?: string;
    kind?: 'rolling' | 'max' | string;
    days?: Primitive;
    start_at?: string | null;
    end_at?: string;
  };
  filters?: ChannelBaselineQuery;
  sample?: {
    scope?: string;
    scan_limit?: Primitive;
    scanned_inventory_count?: Primitive;
    inventory_pagination?: string;
    transactional_snapshot?: boolean;
    inventory_complete?: boolean;
    truncated?: boolean;
    loaded_video_count?: Primitive;
    missing_video_row_count?: Primitive;
    withheld_stale_count?: Primitive;
    excluded_by_availability_count?: Primitive;
    eligible_current_count?: Primitive;
    matched_video_count?: Primitive;
    period_future_excluded_count?: Primitive;
    filtered_out_count?: Primitive;
  };
  statistics?: {
    views?: ChannelBaselineMetricSummary;
    likes?: ChannelBaselineMetricSummary;
    comments?: ChannelBaselineMetricSummary;
    duration_seconds?: ChannelBaselineMetricSummary;
    lifetime_views_per_day?: ChannelBaselineMetricSummary;
    likes_per_1000_views?: ChannelBaselineMetricSummary;
    comments_per_1000_views?: ChannelBaselineMetricSummary;
    title_length_characters?: ChannelBaselineMetricSummary;
    tag_count?: ChannelBaselineMetricSummary;
  };
  publication_cadence?: {
    known_publication_count?: Primitive;
    missing_or_invalid_count?: Primitive;
    future_count?: Primitive;
    first_published_at?: string | null;
    last_published_at?: string | null;
    interval_count?: Primitive;
    mean_gap_days?: Primitive;
    median_gap_days?: Primitive;
    q1_gap_days?: Primitive;
    q3_gap_days?: Primitive;
    min_gap_days?: Primitive;
    max_gap_days?: Primitive;
    publications_last_28_days?: Primitive;
    publications_per_30_days?: Primitive;
  };
  publication_points?: ChannelBaselinePublicationPoint[];
  publication_calendar: ChannelBaselinePublicationCalendar;
  content_breakdown: ChannelBaselineContentBreakdown;
  distributions?: {
    views?: ChannelBaselineBucket[];
    likes?: ChannelBaselineBucket[];
    comments?: ChannelBaselineBucket[];
    lifetime_views_per_day?: ChannelBaselineBucket[];
    likes_per_1000_views?: ChannelBaselineBucket[];
    comments_per_1000_views?: ChannelBaselineBucket[];
    duration_seconds?: ChannelBaselineBucket[];
    publication_years?: ChannelBaselineBucket[];
    publication_quarters?: ChannelBaselineBucket[];
    video_age_days?: ChannelBaselineBucket[];
    categories?: ChannelBaselineCategory[];
    publication_gap_days?: ChannelBaselineBucket[];
    publication_weekdays_utc?: ChannelBaselineBucket[];
    publication_hours_utc?: ChannelBaselineBucket[];
    title_length_characters?: ChannelBaselineBucket[];
    tag_count?: ChannelBaselineBucket[];
    known_category_count?: Primitive;
    unknown_category_count?: Primitive;
  };
  distribution_coverage?: Partial<Record<
    ChannelBaselineDistributionKey,
    ChannelBaselineDistributionCoverage
  >>;
  freshness?: {
    maximum_age_days?: Primitive;
    counters?: { oldest?: string | null; newest?: string | null };
    metadata?: { oldest?: string | null; newest?: string | null };
    category_cache_available?: boolean;
    category_cache_fresh?: boolean;
  };
  methods?: {
    nulls?: string;
    quartiles?: string;
    date_filter?: string;
    counter_totals?: string;
    catalogue_filters_applied?: boolean;
    publication_timezone?: string;
    video_age?: string;
    lifetime_views_per_day?: string;
    per_1000_views?: string;
    publication_gaps?: string;
    title_length?: string;
    tag_count?: string;
    period_semantics?: string;
  };
  source?: {
    kind?: string;
    format_source?: string;
    youtube_requests?: boolean;
    database_writes?: boolean;
    snapshots_used?: boolean;
    availability_history_used?: boolean;
    trends_used?: boolean;
    performance_scores_used?: boolean;
    freshness_enforced?: boolean;
  };
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

export type PipelineHealthSync = SyncRun & {
  age_minutes?: Primitive;
  regular_video_stats?: Record<string, unknown>;
  channel_discovery?: Record<string, unknown>;
};

export type PipelineHealthQueue = {
  scope?: string;
  unit?: string;
  by_type?: Partial<Record<VideoType | 'unknown', PipelineHealthQueue>>;
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
  last_successful_sync?: PipelineHealthSync | null;
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
