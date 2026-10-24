import type {
  ChannelBaselinePeriodKey,
  ChannelBaselinePayload,
  ExplorerChannelLookupResult,
  ChannelExplorerCounterDynamicsPeriodsPayload,
  ChannelExplorerCounterDynamicsStatus,
  ChannelExplorerFormatStatus,
  ChannelExplorerFormatTotalsPayload,
  ChannelExplorerPayload,
  ChannelExplorerVideo,
  ChannelExplorerProfilePayload,
  ChannelVideoGrowthPayload,
  CreatedFolder,
  CreatedLabel,
  ExplorerLabel,
  Folder,
  SidebarData,
  SystemSummary,
  VideoType,
  VideoStaticDetailsPayload,
} from '../types';
import type {
  ExplorerAddChannelResult,
  ExplorerCapabilities,
  ExplorerHomeSummary,
  ExplorerHydrationPayload,
  ExplorerRecentVideosPage,
} from './model';

export const EXPLORER_CONTRACT_VERSION = 1;

export class ExplorerContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ExplorerContractError';
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

type ValueGuard = (value: unknown) => boolean;

const isBoolean: ValueGuard = (value) => typeof value === 'boolean';
const isFalse: ValueGuard = (value) => value === false;
const isTrue: ValueGuard = (value) => value === true;
const isText: ValueGuard = (value) => typeof value === 'string';
const isTextArray: ValueGuard = (value) => (
  Array.isArray(value) && value.every((item) => typeof item === 'string')
);
const isNullableText: ValueGuard = (value) => value === null || typeof value === 'string';
const isFiniteNumber: ValueGuard = (value) => (
  typeof value === 'number' && Number.isFinite(value)
);
const isNonNegativeInteger: ValueGuard = (value) => (
  typeof value === 'number' && Number.isInteger(value) && value >= 0
);
const scanLimitGuard = (coverage: Record<string, unknown>): ValueGuard => (
  coverage.scan_mode === 'full_catalogue'
    ? (value) => value === null
    : isNonNegativeInteger
);
const isNonNegativeIntegerValue: ValueGuard = (value) => (
  isNonNegativeInteger(value)
  || (typeof value === 'string' && /^(?:0|[1-9]\d*)$/.test(value))
);
const isNumericValue: ValueGuard = (value) => (
  value === null
  || isFiniteNumber(value)
  || (
    typeof value === 'string'
    && /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value)
  )
);
const isNonNullNumericValue: ValueGuard = (value) => (
  isFiniteNumber(value)
  || (
    typeof value === 'string'
    && /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value)
  )
);
const isStoredDuration: ValueGuard = (value) => (
  value === null || typeof value === 'string' || isFiniteNumber(value)
);

const CALENDAR_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
const isCalendarDate: ValueGuard = (value) => {
  if (typeof value !== 'string') return false;
  const match = CALENDAR_DATE.exec(value);
  if (!match) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(parsed.getTime())
    && parsed.getUTCFullYear() === Number(match[1])
    && parsed.getUTCMonth() + 1 === Number(match[2])
    && parsed.getUTCDate() === Number(match[3]);
};
const isCalendarYear: ValueGuard = (value) => (
  typeof value === 'number' && Number.isInteger(value) && value >= 1970 && value <= 9999
);


// PostgREST renders timestamptz with +00:00, other payloads with Z: both are UTC.
const UTC_ISO_TIMESTAMP = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(?:Z|\+00:00)$/;

export function utcTimestampMicroseconds(value: unknown): bigint | null {
  if (typeof value !== 'string') return null;
  const match = UTC_ISO_TIMESTAMP.exec(value);
  if (!match) return null;
  const [, year, month, day, hour, minute, second, fraction = ''] = match;
  const wholeSecond = Date.parse(`${year}-${month}-${day}T${hour}:${minute}:${second}Z`);
  if (!Number.isFinite(wholeSecond)) return null;
  const parsed = new Date(wholeSecond);
  if (
    parsed.getUTCFullYear() !== Number(year)
    || parsed.getUTCMonth() + 1 !== Number(month)
    || parsed.getUTCDate() !== Number(day)
    || parsed.getUTCHours() !== Number(hour)
    || parsed.getUTCMinutes() !== Number(minute)
    || parsed.getUTCSeconds() !== Number(second)
  ) return null;
  const fractionalMicroseconds = fraction ? fraction.padEnd(6, '0') : '0';
  return BigInt(wholeSecond) * 1_000n + BigInt(fractionalMicroseconds);
}

function requireRecord(
  parent: Record<string, unknown>,
  key: string,
  contract: string,
): Record<string, unknown> {
  const value = parent[key];
  if (!isRecord(value)) {
    throw new ExplorerContractError(`${contract} is missing a valid ${key} section.`);
  }
  return value;
}

function requireArray(
  parent: Record<string, unknown>,
  key: string,
  contract: string,
): unknown[] {
  const value = parent[key];
  if (!Array.isArray(value)) {
    throw new ExplorerContractError(`${contract} is missing a valid ${key} list.`);
  }
  return value;
}

function validateOptionalFields(
  record: Record<string, unknown>,
  contract: string,
  section: string,
  fields: Record<string, ValueGuard>,
): void {
  for (const [key, guard] of Object.entries(fields)) {
    if (!Object.prototype.hasOwnProperty.call(record, key)) continue;
    if (!guard(record[key])) {
      throw new ExplorerContractError(
        `${contract} returned an invalid ${section}.${key} value.`,
      );
    }
  }
}

function validateRequiredFields(
  record: Record<string, unknown>,
  contract: string,
  section: string,
  fields: Record<string, ValueGuard>,
): void {
  for (const [key, guard] of Object.entries(fields)) {
    if (!Object.prototype.hasOwnProperty.call(record, key) || !guard(record[key])) {
      throw new ExplorerContractError(
        `${contract} returned an invalid ${section}.${key} value.`,
      );
    }
  }
}

function optionalRecord(
  parent: Record<string, unknown>,
  key: string,
  contract: string,
): Record<string, unknown> | undefined {
  if (!Object.prototype.hasOwnProperty.call(parent, key)) return undefined;
  const value = parent[key];
  if (!isRecord(value)) {
    throw new ExplorerContractError(`${contract} returned an invalid ${key} section.`);
  }
  return value;
}

function validateRecordItems(
  values: unknown[],
  contract: string,
  section: string,
  validate: (item: Record<string, unknown>, itemContract: string) => void,
): void {
  values.forEach((value, index) => {
    if (!isRecord(value)) {
      throw new ExplorerContractError(
        `${contract} returned an invalid ${section}[${index}] item.`,
      );
    }
    validate(value, `${section}[${index}]`);
  });
}

function requireVersion(payload: unknown, contract: string): Record<string, unknown> {
  if (!isRecord(payload)) {
    throw new ExplorerContractError(`${contract} returned an invalid payload.`);
  }
  if (payload.contract_version !== EXPLORER_CONTRACT_VERSION) {
    throw new ExplorerContractError(
      `${contract} contract version is not supported by this frontend.`,
    );
  }
  return payload;
}

function requireSource(
  payload: Record<string, unknown>,
  contract: string,
  kind: string,
  invariants: Record<string, unknown>,
): void {
  const source = requireRecord(payload, 'source', contract);
  if (source.kind !== kind) {
    throw new ExplorerContractError(`${contract} returned an unexpected data source.`);
  }
  for (const [key, expected] of Object.entries(invariants)) {
    if (source[key] !== expected) {
      throw new ExplorerContractError(
        `${contract} returned an incompatible source.${key} invariant.`,
      );
    }
  }
}

function validateFilters(
  payload: Record<string, unknown>,
  contract: string,
  options: { pagination: boolean },
): void {
  const filters = optionalRecord(payload, 'filters', contract);
  if (!filters) return;
  validateOptionalFields(filters, contract, 'filters', {
    q: isText,
    category_id: isText,
    published_from: isNullableText,
    published_before: isNullableText,
    min_views: isNumericValue,
    max_views: isNumericValue,
    min_duration_seconds: isNumericValue,
    max_duration_seconds: isNumericValue,
    ...(options.pagination ? {
      sort_by: isText,
      sort_order: isText,
      limit: isNonNegativeInteger,
      offset: isNonNegativeInteger,
    } : {}),
  });
}

function validateMetricSummary(
  value: Record<string, unknown>,
  contract: string,
  section: string,
): void {
  validateOptionalFields(value, contract, section, {
    known_count: isNonNegativeInteger,
    missing_count: isNonNegativeInteger,
    total: isNumericValue,
    mean: isNumericValue,
    median: isNumericValue,
    q1: isNumericValue,
    q3: isNumericValue,
    min: isNumericValue,
    max: isNumericValue,
  });
}

function validateFreshnessRange(
  value: Record<string, unknown>,
  contract: string,
  section: string,
): void {
  validateOptionalFields(value, contract, section, {
    oldest: isNullableText,
    newest: isNullableText,
  });
}

export function parseExplorerCapabilities(payload: unknown): ExplorerCapabilities {
  const root = requireVersion(payload, 'Explorer capabilities');
  const explorer = requireRecord(root, 'channel_explorer', 'Explorer capabilities');
  if (explorer.raw !== 'available') {
    throw new ExplorerContractError(
      'Explorer capabilities returned an invalid raw state.',
    );
  }
  const allowed = new Set(['available', 'unavailable']);
  for (const key of ['baseline', 'measured_change']) {
    if (typeof explorer[key] !== 'string' || !allowed.has(explorer[key])) {
      throw new ExplorerContractError(
        `Explorer capabilities returned an invalid ${key} state.`,
      );
    }
  }
  const timeline = explorer.timeline ?? 'unavailable';
  if (typeof timeline !== 'string' || !allowed.has(timeline)) {
    throw new ExplorerContractError(
      'Explorer capabilities returned an invalid timeline state.',
    );
  }
  const videoGrowth = explorer.video_growth ?? 'unavailable';
  const filterFacets = explorer.filter_facets ?? 'unavailable';
  const staticDetails = explorer.static_video_details ?? 'unavailable';
  const videoHistory = explorer.video_history ?? 'unavailable';
  const videoLaunch = explorer.video_launch ?? 'unavailable';
  if (typeof videoLaunch !== 'string' || !allowed.has(videoLaunch)) {
    throw new ExplorerContractError('Explorer capabilities returned an invalid video_launch state.');
  }
  if (typeof videoHistory !== 'string' || !allowed.has(videoHistory)) {
    throw new ExplorerContractError('Explorer capabilities returned an invalid video_history state.');
  }
  if (typeof staticDetails !== 'string' || !allowed.has(staticDetails)) {
    throw new ExplorerContractError('Explorer capabilities returned an invalid static_video_details state.');
  }
  if (typeof filterFacets !== 'string' || !allowed.has(filterFacets)) {
    throw new ExplorerContractError('Explorer capabilities returned an invalid filter_facets state.');
  }
  if (typeof videoGrowth !== 'string' || !allowed.has(videoGrowth)) {
    throw new ExplorerContractError(
      'Explorer capabilities returned an invalid video_growth state.',
    );
  }
  return {
    ...root,
    channel_explorer: {
      ...explorer,
      timeline,
      video_growth: videoGrowth,
      filter_facets: filterFacets,
      static_video_details: staticDetails,
      video_history: videoHistory,
      video_launch: videoLaunch,
    },
  } as ExplorerCapabilities;
}

export function parseSidebarData(payload: unknown): SidebarData {
  if (!isRecord(payload)) {
    throw new ExplorerContractError('Channel list returned an invalid payload.');
  }
  if (
    !Array.isArray(payload.channels)
    || !Array.isArray(payload.folders)
    || !Array.isArray(payload.labels)
  ) {
    throw new ExplorerContractError('Channel list is missing channels, folders, or labels.');
  }
  validateRecordItems(
    payload.channels,
    'Channel list',
    'channels',
    (channel, section) => validateOptionalFields(channel, 'Channel list', section, {
      channel_id: isText,
      title: isNullableText,
      avatar_url: isNullableText,
      folder_id: isNullableText,
      added_at: isNullableText,
      subscribers: isNumericValue,
      video_count: isNumericValue,
      label_keys: isTextArray,
    }),
  );
  validateRecordItems(
    payload.folders,
    'Channel list',
    'folders',
    (folder, section) => validateOptionalFields(folder, 'Channel list', section, {
      id: isText,
      name: isText,
    }),
  );
  validateRecordItems(
    payload.labels,
    'Channel list',
    'labels',
    (label, section) => validateRequiredFields(label, 'Channel list', section, {
      label_key: isText,
      name: isText,
      label_type: (value) => value === 'preset' || value === 'custom',
    }),
  );
  return payload as SidebarData;
}

const EXPLORER_HYDRATION_STATUSES = new Set([
  'idle',
  'confirmation_required',
  'queued',
  'running',
  'ready',
  'completed',
  'blocked',
  'error',
  'suspended',
]);
const EXPLORER_HYDRATION_PHASES = new Set([
  'inventory',
  'hydrate_videos',
  'verify',
  'ready',
]);
const EXPLORER_HYDRATION_ACTIONS = new Set([
  'confirm',
  'start',
  'retry',
]);

export function parseExplorerHydration(
  payload: unknown,
  expectedChannelId?: string,
): ExplorerHydrationPayload {
  const contract = 'Channel hydration';
  if (!isRecord(payload) || (payload.contract_version !== 1 && payload.contract_version !== 2)) {
    throw new ExplorerContractError(`${contract} contract version is not supported by this frontend.`);
  }
  const root = payload;
  const current = root.contract_version === 2;
  const nullableCount: ValueGuard = (value) => value === null || isNonNegativeInteger(value);
  validateRequiredFields(root, contract, 'response', {
    channel_id: isText,
    status: (value) => typeof value === 'string' && EXPLORER_HYDRATION_STATUSES.has(value),
    phase: (value) => value === null
      || (typeof value === 'string' && EXPLORER_HYDRATION_PHASES.has(value)),
    current_profile_ready: isBoolean,
    inventory_ready: isBoolean,
    progress: isRecord,
    estimate: (value) => value === null || isRecord(value),
    confirmation_required: isBoolean,
    action_required: (value) => value === null
      || (typeof value === 'string' && EXPLORER_HYDRATION_ACTIONS.has(value)),
    job: (value) => value === null || isRecord(value),
  });
  const progress = requireRecord(root, 'progress', contract);
  validateRequiredFields(progress, contract, 'progress', {
    processed: isNonNegativeInteger,
    total: current ? nullableCount : isNonNegativeInteger,
    percent: (value) => (current && value === null)
      || (isFiniteNumber(value) && Number(value) >= 0 && Number(value) <= 100),
    ...(current ? { discovered: isNonNegativeInteger } : {}),
  });
  if (progress.total === null && progress.percent !== null) {
    throw new ExplorerContractError(`${contract} returned a percentage for an unknown total.`);
  }
  if (progress.total !== null && Number(progress.processed) > Number(progress.total)) {
    throw new ExplorerContractError(`${contract} returned progress beyond its total.`);
  }
  if (root.estimate !== null) {
    const estimate = requireRecord(root, 'estimate', contract);
    validateRequiredFields(estimate, contract, 'estimate', {
      items: current ? nullableCount : isNonNegativeInteger,
      pages: current ? nullableCount : isNonNegativeInteger,
      quota_units: current ? nullableCount : isNonNegativeInteger,
      ...(current ? {
        estimate_kind: (value) => value === 'upper_bound' || value === 'unknown' || value === 'exact_workset',
        items_lower_bound: isNonNegativeInteger,
        items_upper_bound: nullableCount,
      } : {}),
    });
    validateOptionalFields(estimate, contract, 'estimate', {
      quota_basis: value => value === 'before_start',
    });
    if (estimate.quota_basis === 'before_start' && estimate.quota_units === null) {
      throw new ExplorerContractError(`${contract} returned an unknown initial quota estimate.`);
    }
    if (current) {
      const unknown = estimate.estimate_kind === 'unknown';
      if (unknown ? (
        estimate.items !== null || estimate.pages !== null
        || estimate.quota_units !== null || estimate.items_upper_bound !== null
      ) : (
        estimate.items === null || estimate.pages === null || estimate.quota_units === null
        || estimate.items_upper_bound !== estimate.items
        || Number(estimate.items_lower_bound) > Number(estimate.items_upper_bound)
        || (estimate.estimate_kind === 'exact_workset' && estimate.items_lower_bound !== estimate.items)
      )) {
        throw new ExplorerContractError(`${contract} returned inconsistent estimate bounds.`);
      }
    }
  }
  if (root.current_profile_ready === true && root.inventory_ready !== true) {
    throw new ExplorerContractError(`${contract} marked a profile ready before inventory.`);
  }
  validateOptionalFields(root, contract, 'response', {
    all_initial_checks_complete: isBoolean,
  });
  if (
    root.status === 'completed'
    && (
      root.phase !== 'verify'
      || root.all_initial_checks_complete !== true
      || root.confirmation_required !== false
      || root.action_required !== null
      || !isRecord(root.job)
      || root.job.status !== 'completed'
      || root.job.phase !== 'verify'
    )
  ) {
    throw new ExplorerContractError(`${contract} returned an unverified completion state.`);
  }
  if (
    root.confirmation_required !== (root.status === 'confirmation_required')
    || (root.confirmation_required === true && root.estimate === null)
    || (root.confirmation_required === true && root.action_required !== 'confirm')
    || (root.action_required === 'confirm' && root.confirmation_required !== true)
    || (root.action_required !== null && root.estimate === null)
  ) {
    throw new ExplorerContractError(`${contract} returned inconsistent confirmation state.`);
  }
  if (
    root.status === 'ready'
    && (
      root.phase !== 'ready'
      || root.current_profile_ready !== true
      || root.inventory_ready !== true
      || root.action_required !== null
    )
  ) {
    throw new ExplorerContractError(`${contract} returned an incomplete ready state.`);
  }
  if (expectedChannelId !== undefined && root.channel_id !== expectedChannelId) {
    throw new ExplorerContractError(`${contract} returned data for another channel.`);
  }
  return root as ExplorerHydrationPayload;
}

export function parseExplorerAddChannel(payload: unknown): ExplorerAddChannelResult {
  if (!isRecord(payload)) {
    throw new ExplorerContractError('Add channel returned an invalid payload.');
  }
  validateOptionalFields(payload, 'Add channel', 'response', {
    error: isText,
    detail: isText,
    code: isText,
    title: isText,
    hydration: isRecord,
  });
  if (typeof payload.error === 'string' || typeof payload.detail === 'string') {
    return payload as ExplorerAddChannelResult;
  }
  if (!['added', 'restored', 'exists'].includes(String(payload.status || ''))) {
    throw new ExplorerContractError('Add channel returned an unknown status.');
  }
  const channel = requireRecord(payload, 'channel', 'Add channel');
  validateOptionalFields(channel, 'Add channel', 'channel', {
    channel_id: isText,
    title: isText,
    avatar_url: isNullableText,
    folder_id: isNullableText,
    subscribers: isNumericValue,
    video_count: isNumericValue,
  });
  if (!channel.channel_id) {
    throw new ExplorerContractError('Add channel is missing the saved channel ID.');
  }
  if (payload.hydration !== undefined) {
    const hydration = parseExplorerHydration(payload.hydration);
    if (hydration.channel_id !== channel.channel_id) {
      throw new ExplorerContractError('Add channel returned hydration for another channel.');
    }
  }
  return payload as ExplorerAddChannelResult;
}

export function parseExplorerChannelLookup(payload: unknown): ExplorerChannelLookupResult {
  const contract = 'Channel Library link lookup';
  const root = requireVersion(payload, contract);
  if (root.status !== 'matched' && root.status !== 'not_tracked') {
    throw new ExplorerContractError(`${contract} returned an unknown status.`);
  }
  validateRequiredFields(root, contract, 'response', {
    video_id: (value) => typeof value === 'string' && /^[a-zA-Z0-9_-]{11}$/.test(value),
    channel_id: isNullableText,
  });
  if (
    (root.status === 'matched' && !root.channel_id)
    || (root.status === 'not_tracked' && root.channel_id !== null)
  ) {
    throw new ExplorerContractError(`${contract} returned an inconsistent channel match.`);
  }
  requireSource(root, contract, 'stored_video_channel_mapping', {
    youtube_requests: false,
    database_writes: false,
  });
  return root as ExplorerChannelLookupResult;
}

export function parseExplorerHomeSummary(payload: unknown): ExplorerHomeSummary {
  const contract = 'Explorer home summary';
  const root = requireVersion(payload, contract);
  requireSource(root, contract, 'stored_typed_video_inventory', {
    format_source: 'video_inventory.video_type',
    scope: 'visible_channels',
    youtube_requests: false,
    database_writes: false,
  });
  if (!Object.prototype.hasOwnProperty.call(root, 'last_successful_sync_at')
      || !isNullableText(root.last_successful_sync_at)) {
    throw new ExplorerContractError(`${contract} returned an invalid last_successful_sync_at.`);
  }
  if (!Object.prototype.hasOwnProperty.call(root, 'last_data_change_at')
      || !isNullableText(root.last_data_change_at)) {
    throw new ExplorerContractError(`${contract} returned an invalid last_data_change_at.`);
  }
  if (!Object.prototype.hasOwnProperty.call(root, 'saved_videos')) {
    throw new ExplorerContractError(`${contract} is missing saved_videos.`);
  }
  if (root.saved_videos === null) return root as ExplorerHomeSummary;
  if (!isRecord(root.saved_videos)) {
    throw new ExplorerContractError(`${contract} returned an invalid saved_videos section.`);
  }
  for (const key of ['total', 'long', 'short', 'stream']) {
    if (!isNonNegativeInteger(root.saved_videos[key])) {
      throw new ExplorerContractError(
        `${contract} returned an invalid saved_videos.${key} value.`,
      );
    }
  }
  const typedTotal = Number(root.saved_videos.long)
    + Number(root.saved_videos.short)
    + Number(root.saved_videos.stream);
  if (root.saved_videos.total !== typedTotal) {
    throw new ExplorerContractError(
      `${contract} returned inconsistent typed saved-video counts.`,
    );
  }
  return root as ExplorerHomeSummary;
}

export function parseExplorerRecentVideos(payload: unknown): ExplorerRecentVideosPage {
  const contract = 'Explorer recent videos';
  const root = requireVersion(payload, contract);
  requireSource(root, contract, 'stored_recent_long_form_videos', {
    format_source: 'video_inventory.video_type',
    scope: 'visible_channels',
    youtube_requests: false,
    database_writes: false,
  });
  if (!new Set(['24h', '7d', '28d']).has(String(root.window))) {
    throw new ExplorerContractError(`${contract} returned an invalid window.`);
  }
  for (const key of ['generated_at', 'published_from']) {
    if (!isText(root[key])) {
      throw new ExplorerContractError(`${contract} returned an invalid ${key}.`);
    }
  }
  if (!isBoolean(root.has_more) || !isNullableText(root.next_cursor)) {
    throw new ExplorerContractError(`${contract} returned invalid pagination.`);
  }
  if (root.has_more && !root.next_cursor) {
    throw new ExplorerContractError(`${contract} is missing its next cursor.`);
  }
  const items = requireArray(root, 'items', contract);
  validateRecordItems(items, contract, 'items', (item, section) => {
    const fields: Record<string, ValueGuard> = {
      video_id: isText,
      channel_id: isText,
      title: isNullableText,
      thumbnail_url: isNullableText,
      published_at: isText,
      checked_at: isNullableText,
      views: isNumericValue,
      like_count: isNumericValue,
      comment_count: isNumericValue,
    };
    for (const [key, guard] of Object.entries(fields)) {
      if (!Object.prototype.hasOwnProperty.call(item, key) || !guard(item[key])) {
        throw new ExplorerContractError(
          `${contract} returned an invalid ${section}.${key} value.`,
        );
      }
    }
  });
  return root as ExplorerRecentVideosPage;
}

function validateSyncRun(
  run: Record<string, unknown>,
  contract: string,
  section: string,
): void {
  validateOptionalFields(run, contract, section, {
    status: isText,
    started_at: isNullableText,
    finished_at: isNullableText,
    errors: isNumericValue,
  });
}

export function parseExplorerSystemSummary(payload: unknown): SystemSummary {
  const contract = 'Explorer system summary';
  if (!isRecord(payload)) {
    throw new ExplorerContractError(`${contract} returned an invalid payload.`);
  }
  const allowedStatuses = new Set(['ok', 'running', 'warning', 'error', 'unknown', 'paused']);
  if (typeof payload.status !== 'string' || !allowedStatuses.has(payload.status)) {
    throw new ExplorerContractError(`${contract} returned an invalid status.`);
  }
  validateOptionalFields(payload, contract, 'payload', {
    status_reason: isText,
    now: isText,
  });

  const version = payload.version;
  if (version !== undefined && (typeof version !== 'string' || version.trim() !== version || version.length > 255
    || !/^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/.test(version))) {
    throw new ExplorerContractError(`${contract} returned an invalid version.`);
  }

  const warnings = requireArray(payload, 'warnings', contract);
  validateRecordItems(warnings, contract, 'warnings', (warning, section) => {
    validateOptionalFields(warning, contract, section, {
      code: isText,
      message: isText,
      count: isNumericValue,
    });
  });

  if (!Object.prototype.hasOwnProperty.call(payload, 'latest_run')) {
    throw new ExplorerContractError(`${contract} is missing latest_run.`);
  }
  if (payload.latest_run !== null) {
    if (!isRecord(payload.latest_run)) {
      throw new ExplorerContractError(`${contract} returned an invalid latest_run section.`);
    }
    validateSyncRun(payload.latest_run, contract, 'latest_run');
  }

  const recentRuns = requireArray(payload, 'recent_runs', contract);
  validateRecordItems(recentRuns, contract, 'recent_runs', (run, section) => {
    validateSyncRun(run, contract, section);
  });
  return payload as SystemSummary;
}

/** Derived static values belong to the exact frozen browse session. */
export function parseVideoStaticDetailsV1(payload: unknown, channelId: string, videoId: string, browseSession: string): VideoStaticDetailsPayload {
  const contract = 'Video static details';
  if (!isRecord(payload)) throw new ExplorerContractError('The server returned incompatible video static details.');
  const root = payload;
  const metrics = requireRecord(root, 'metrics', contract), raw = requireRecord(root, 'raw', contract);
  const method = requireRecord(root, 'method', contract), source = requireRecord(root, 'source', contract);
  const generated = utcTimestampMicroseconds(root.generated_at), expires = utcTimestampMicroseconds(root.expires_at);
  const nullableCounter: ValueGuard = (value) => value === null || (Number.isSafeInteger(value) && Number(value) >= 0);
  const nullableRate: ValueGuard = (value) => value === null || (typeof value === 'number' && Number.isFinite(value) && value >= 0);
  validateRequiredFields(metrics, contract, 'metrics', { like_rate_percent: nullableRate, comment_rate_percent: nullableRate,
    title_length_characters: nullableCounter, tag_count: nullableCounter });
  if (root.contract_version !== 1 || root.channel_id !== channelId || root.video_id !== videoId || root.browse_session !== browseSession
    || generated === null || expires === null || expires <= generated
    || !(raw.tags === null || Array.isArray(raw.tags) && raw.tags.every((tag) => typeof tag === 'string'))
    || !(raw.metadata_checked_at === null || utcTimestampMicroseconds(raw.metadata_checked_at) !== null)
    || (raw.tags === null ? metrics.tag_count !== null : metrics.tag_count !== (raw.tags as string[]).length)
    || method.rates !== 'counter / views * 100' || method.zero_views !== 'unavailable'
    || method.title_length !== 'unicode_code_points' || method.tag_count !== 'stored_array_length'
    || source.kind !== 'derived_video_static_details' || source.attribution !== 'Leti Cook'
    || source.snapshots_used !== false || source.youtube_requests !== false || source.database_writes !== false) {
    throw new ExplorerContractError('The server returned incompatible video static details.');
  }
  return root as unknown as VideoStaticDetailsPayload;
}

/** The same current-row fields are reused by Saved collection members. */
export function parseStoredExplorerVideo(value: unknown): ChannelExplorerVideo {
  if (!isRecord(value)) throw new ExplorerContractError('Stored video must be an object.');
  validateOptionalFields(value, 'Stored video', 'video', {
    video_id: isNullableText, title: isNullableText, thumbnail_url: isNullableText,
    published_at: isNullableText, checked_at: isNullableText, duration: isStoredDuration,
    views: isNumericValue, like_count: isNumericValue, comment_count: isNumericValue,
    category_id: isNullableText, category_name: isNullableText,
    tags: (value) => value === null || isTextArray(value),
  });
  return value as ChannelExplorerVideo;
}

export function parseRawExplorerV1(
  payload: unknown,
  expectedVideoType: VideoType = 'long',
): ChannelExplorerPayload {
  const root = requireVersion(payload, 'Current Explorer data');
  const contract = 'Current Explorer data';
  if (root.video_type !== expectedVideoType) {
    throw new ExplorerContractError(
      `${contract} returned an unexpected video_type.`,
    );
  }
  const channel = requireRecord(root, 'channel', contract);
  validateOptionalFields(channel, contract, 'channel', {
    channel_id: isNullableText,
    title: isNullableText,
    subscribers: isNumericValue,
    views: isNumericValue,
    video_count: isNumericValue,
    country_code: isNullableText,
    channel_metadata_checked_at: isNullableText,
  });
  validateFilters(root, contract, { pagination: true });

  const coverage = requireRecord(root, 'coverage', contract);
  if (coverage.video_type !== expectedVideoType) {
    throw new ExplorerContractError(
      `${contract} returned an incompatible coverage.video_type.`,
    );
  }
  validateOptionalFields(coverage, contract, 'coverage', {
    scope: isText,
    video_type: isText,
    scan_limit: scanLimitGuard(coverage),
    scanned_count: isNonNegativeInteger,
    inventory_status: isText,
    inventory_ready: isBoolean,
    product_exposure_ready: isBoolean,
    inventory_complete: isBoolean,
    inventory_source_checked_at: isNullableText,
    type_resolution_withheld: isNonNegativeInteger,
    loaded_video_count: isNonNegativeInteger,
    missing_video_row_count: isNonNegativeInteger,
    inventory_pagination: isText,
    transactional_snapshot: isFalse,
    complete: isBoolean,
    truncated: isBoolean,
    category_cache_available: isBoolean,
    category_cache_fresh: isBoolean,
    category_cache_refreshed_at: isNullableText,
    current_data_max_age_days: isNonNegativeInteger,
    channel_data_fresh: isBoolean,
    video_rows_withheld_stale: isNonNegativeInteger,
    video_rows_withheld_without_stream_completion_evidence: isNonNegativeInteger,
    video_rows_with_stale_counters: isNonNegativeInteger,
    video_rows_with_stale_metadata: isNonNegativeInteger,
  });

  const pagination = requireRecord(root, 'pagination', contract);
  validateOptionalFields(pagination, contract, 'pagination', {
    limit: isNonNegativeInteger,
    offset: isNonNegativeInteger,
    returned_count: isNonNegativeInteger,
    has_more_in_scan: isBoolean,
  });

  const options = requireRecord(root, 'options', 'Current Explorer data');
  const categories = requireArray(options, 'categories', contract);
  validateRecordItems(categories, contract, 'options.categories', (category, section) => {
    validateOptionalFields(category, contract, section, {
      category_id: isText,
      category_name: isText,
    });
  });
  if (Object.prototype.hasOwnProperty.call(options, 'sort_fields')) {
    if (!Array.isArray(options.sort_fields) || !options.sort_fields.every(isText)) {
      throw new ExplorerContractError(`${contract} returned invalid options.sort_fields items.`);
    }
  }

  const videos = requireArray(root, 'videos', contract);
  validateRecordItems(videos, contract, 'videos', (video, section) => {
    validateOptionalFields(video, contract, section, {
      video_id: isNullableText,
      title: isNullableText,
      thumbnail_url: isNullableText,
      published_at: isNullableText,
      checked_at: isNullableText,
      duration: isStoredDuration,
      views: isNumericValue,
      like_count: isNumericValue,
      comment_count: isNumericValue,
      category_id: isNullableText,
      category_name: isNullableText,
      tags: (value) => value === null || isTextArray(value),
    });
  });
  requireSource(root, contract, 'stored_current_youtube_fields', {
    youtube_requests: false,
    database_writes: false,
    snapshots_used: false,
    derived_performance_metrics: false,
    freshness_enforced: true,
  });
  return root as ChannelExplorerPayload;
}

export function parseVideoGrowthV1(
  payload: unknown,
  expectedChannelId?: string,
): ChannelVideoGrowthPayload {
  const contract = 'Video growth catalogue';
  const root = requireVersion(payload, contract);
  if (!isText(root.generated_at)) {
    throw new ExplorerContractError(`${contract} returned an invalid generated_at.`);
  }

  const channel = requireRecord(root, 'channel', contract);
  validateOptionalFields(channel, contract, 'channel', {
    channel_id: isNullableText,
    title: isNullableText,
    subscribers: isNumericValue,
    views: isNumericValue,
    video_count: isNumericValue,
    country_code: isNullableText,
    channel_metadata_checked_at: isNullableText,
  });
  if (expectedChannelId && channel.channel_id !== expectedChannelId) {
    throw new ExplorerContractError(`${contract} returned a different channel than requested.`);
  }
  requireRecord(root, 'filters', contract);
  validateFilters(root, contract, { pagination: true });

  const coverage = requireRecord(root, 'coverage', contract);
  validateOptionalFields(coverage, contract, 'coverage', {
    scope: isText,
    scan_limit: scanLimitGuard(coverage),
    scanned_count: isNonNegativeInteger,
    inventory_pagination: isText,
    transactional_snapshot: isFalse,
    complete: isBoolean,
    truncated: isBoolean,
    category_cache_available: isBoolean,
    category_cache_fresh: isBoolean,
    category_cache_refreshed_at: isNullableText,
    current_data_max_age_days: isNonNegativeInteger,
    channel_data_fresh: isBoolean,
    video_rows_withheld_stale: isNonNegativeInteger,
    video_rows_with_stale_counters: isNonNegativeInteger,
    video_rows_with_stale_metadata: isNonNegativeInteger,
  });
  const growthCoverage = requireRecord(coverage, 'growth', contract);
  validateRequiredFields(growthCoverage, contract, 'coverage.growth', {
    requested_video_count: isNonNegativeInteger,
    returned_growth_row_count: isNonNegativeInteger,
    calculable_count: isNonNegativeInteger,
    ready_count: isNonNegativeInteger,
    partial_count: isNonNegativeInteger,
    started_today_count: isNonNegativeInteger,
    waiting_next_snapshot_count: isNonNegativeInteger,
    unavailable_count: isNonNegativeInteger,
  });

  const pagination = requireRecord(root, 'pagination', contract);
  validateOptionalFields(pagination, contract, 'pagination', {
    limit: isNonNegativeInteger,
    offset: isNonNegativeInteger,
    returned_count: isNonNegativeInteger,
    has_more_in_scan: isBoolean,
  });

  const options = requireRecord(root, 'options', contract);
  const categories = requireArray(options, 'categories', contract);
  validateRecordItems(categories, contract, 'options.categories', (category, section) => {
    validateOptionalFields(category, contract, section, {
      category_id: isText,
      category_name: isText,
    });
  });
  const sortFields = requireArray(options, 'sort_fields', contract);
  if (!sortFields.every(isText) || !sortFields.includes('views_gained')) {
    throw new ExplorerContractError(`${contract} returned incompatible sort fields.`);
  }

  const statuses = new Set([
    'ready',
    'partial',
    'started_today',
    'waiting_next_snapshot',
    'unavailable',
  ]);
  const nullableNonNegativeInteger: ValueGuard = (value) => (
    value === null || isNonNegativeInteger(value)
  );
  const nonNullJsonInteger: ValueGuard = (value) => (
    (typeof value === 'number' && Number.isSafeInteger(value) && !Object.is(value, -0))
    || (typeof value === 'string' && /^(?:0|[1-9]\d*|-[1-9]\d*)$/.test(value))
  );
  const jsonInteger: ValueGuard = (value) => value === null || nonNullJsonInteger(value);
  const videos = requireArray(root, 'videos', contract);
  validateRecordItems(videos, contract, 'videos', (video, section) => {
    validateOptionalFields(video, contract, section, {
      video_id: isNullableText,
      title: isNullableText,
      thumbnail_url: isNullableText,
      published_at: isNullableText,
      checked_at: isNullableText,
      duration: isStoredDuration,
      views: isNumericValue,
      like_count: isNumericValue,
      comment_count: isNumericValue,
      category_id: isNullableText,
      category_name: isNullableText,
      tags: (value) => value === null || isTextArray(value),
    });
    const growth = requireRecord(video, 'view_growth', `${contract} ${section}`);
    validateRequiredFields(growth, contract, `${section}.view_growth`, {
      status: (value) => typeof value === 'string' && statuses.has(value),
      delta_views: jsonInteger,
      tracked_days: nullableNonNegativeInteger,
      observed_start_at: isNullableText,
      observed_end_at: isNullableText,
      actual_interval_seconds: nullableNonNegativeInteger,
      boundary_distance_seconds: nullableNonNegativeInteger,
      reason: isNullableText,
    });
    validateOptionalFields(growth, contract, `${section}.view_growth`, {
      average_views_per_day: (value) => value === null || (
        typeof value === 'string' && /^-?(?:0|[1-9]\d*)(?:\.\d{1,2})?$/.test(value)
        && !/^-0(?:\.0+)?$/.test(value)
      ),
    });
    if (growth.average_views_per_day != null && (
      !['ready', 'partial'].includes(String(growth.status))
      || typeof growth.actual_interval_seconds !== 'number' || growth.actual_interval_seconds <= 0
      || !isRecord(root.methods)
      || root.methods.average_views_per_day !== 'signed_delta_per_actual_second_times_86400_rounded_2dp_half_even'
    )) throw new ExplorerContractError(`${contract} returned an unsupported average growth rate.`);

    if (growth.status === 'ready') {
      if (
        !nonNullJsonInteger(growth.delta_views)
        || !isText(growth.observed_start_at)
        || !isText(growth.observed_end_at)
        || !isNonNegativeInteger(growth.actual_interval_seconds)
        || !isNonNegativeInteger(growth.boundary_distance_seconds)
        || growth.tracked_days !== null
        || growth.reason !== null
      ) throw new ExplorerContractError(`${contract} returned an invalid ready growth row.`);
    } else if (growth.status === 'partial') {
      if (
        !nonNullJsonInteger(growth.delta_views)
        || !isText(growth.observed_start_at)
        || !isText(growth.observed_end_at)
        || !isNonNegativeInteger(growth.actual_interval_seconds)
        || !isNonNegativeInteger(growth.tracked_days)
        || growth.tracked_days === 0
        || growth.boundary_distance_seconds !== null
        || !isText(growth.reason)
      ) throw new ExplorerContractError(`${contract} returned an invalid partial growth row.`);
    } else if (growth.status === 'started_today' || growth.status === 'waiting_next_snapshot') {
      if (
        growth.delta_views !== null
        || growth.tracked_days !== null
        || growth.boundary_distance_seconds !== null
        || !isText(growth.observed_end_at)
        || !isText(growth.reason)
      ) throw new ExplorerContractError(`${contract} returned an invalid tracking growth row.`);
    } else if (
      growth.delta_views !== null
      || growth.tracked_days !== null
      || growth.boundary_distance_seconds !== null
      || !isText(growth.reason)
    ) {
      throw new ExplorerContractError(`${contract} returned an invalid unavailable growth row.`);
    }
  });

  const window = requireRecord(root, 'window', contract);
  validateRequiredFields(window, contract, 'window', {
    metric: (value) => value === 'views',
    requested_days: (value) => value === 28,
    boundary_tolerance_days: (value) => value === 0,
    endpoint: (value) => value === 'per_video_current_checked_at',
  });
  const methods = requireRecord(root, 'methods', contract);
  const requestedFilters = isRecord(root.filters) ? root.filters.catalogue_filters : undefined;
  const growthFiltered = isRecord(requestedFilters) && isRecord(requestedFilters.growth)
    && Object.keys(requestedFilters.growth).length > 0;
  validateRequiredFields(methods, contract, 'methods', {
    endpoint_observation: (value) => value === 'current_video_row_with_matching_snapshot',
    full_window_start: (value) => value === 'exact_28d_interval_inside_hot_window',
    partial_window_start: (value) => value === 'earliest_snapshot_inside_hot_window',
    partial_minimum_seconds: (value) => value === 86400,
    counter_decrease: (value) => value === 'reported_as_correction_not_clamped',
    sorting: (value) => value === 'signed_delta_ready_and_partial_nulls_last_then_video_id',
    filters_applied_before_growth_read: (value) => value === !growthFiltered,
    pagination_applied_after_sorting: isTrue,
  });
  validateOptionalFields(methods, contract, 'methods', {
    average_views_per_day: (value) => value === 'signed_delta_per_actual_second_times_86400_rounded_2dp_half_even',
  });
  if (growthFiltered) validateRequiredFields(methods, contract, 'methods', {
    growth_filters_applied_after_growth_read: isTrue,
    read_scope: (value) => value === 'full_eligible_catalogue_for_contextual_facets',
  });
  requireSource(root, contract, 'derived_video_view_growth_from_stored_snapshots', {
    format_source: 'video_inventory.video_type',
    youtube_requests: false,
    database_writes: false,
    snapshots_used: true,
    availability_history_used: false,
    derived_performance_metrics: true,
    forecasts_used: false,
    performance_ranks_used: false,
    freshness_enforced: true,
  });
  return root as ChannelVideoGrowthPayload;
}

export function parseExplorerProfileV1(
  payload: unknown,
  expectedChannelId?: string,
): ChannelExplorerProfilePayload {
  const contract = 'Explorer channel profile';
  const root = requireVersion(payload, contract);
  if (!isText(root.generated_at)) {
    throw new ExplorerContractError(`${contract} returned an invalid generated_at.`);
  }
  const channel = requireRecord(root, 'channel', contract);
  validateRequiredFields(channel, contract, 'channel', {
    channel_id: isText,
    title: isNullableText,
    subscribers: isNumericValue,
    views: isNumericValue,
    video_count: isNumericValue,
    country_code: isNullableText,
    channel_created_at: isNullableText,
    channel_metadata_checked_at: isNullableText,
  });
  if (expectedChannelId && channel.channel_id !== expectedChannelId) {
    throw new ExplorerContractError(`${contract} returned a different channel than requested.`);
  }
  if (root.public_video_count_notice !== undefined && root.public_video_count_notice !== null) {
    const notice = requireRecord(root, 'public_video_count_notice', contract);
    validateRequiredFields(notice, contract, 'public_video_count_notice', {
      code: (value) => value === 'public_video_count_decreased',
      checked_at: isText,
      source_expires_at: isText,
    });
  }

  const freshness = requireRecord(root, 'freshness', contract);
  validateRequiredFields(freshness, contract, 'freshness', {
    maximum_age_days: isNonNegativeInteger,
    channel_data_fresh: isBoolean,
  });
  requireSource(root, contract, 'stored_channel_profile', {
    youtube_requests: false,
    database_writes: false,
  });
  return root as ChannelExplorerProfilePayload;
}

export function parseExplorerFolder(payload: unknown): CreatedFolder {
  const contract = 'Explorer folder mutation';
  if (!isRecord(payload)) {
    throw new ExplorerContractError(`${contract} returned a non-object payload.`);
  }
  validateRequiredFields(payload, contract, 'folder', {
    id: isText,
    name: isText,
  });
  if (
    typeof payload.id !== 'string'
    || typeof payload.name !== 'string'
    || !payload.id.trim()
    || !payload.name.trim()
  ) {
    throw new ExplorerContractError(`${contract} returned an empty id or name.`);
  }
  if (payload.created !== undefined && typeof payload.created !== 'boolean') {
    throw new ExplorerContractError(`${contract} returned an invalid created flag.`);
  }
  return payload as CreatedFolder;
}

export function parseExplorerLabel(payload: unknown): CreatedLabel {
  const contract = 'Explorer label mutation';
  if (!isRecord(payload)) {
    throw new ExplorerContractError(`${contract} returned a non-object payload.`);
  }
  validateRequiredFields(payload, contract, 'label', {
    label_key: isText,
    name: isText,
    label_type: (value) => value === 'preset' || value === 'custom',
  });
  if (
    typeof payload.label_key !== 'string'
    || typeof payload.name !== 'string'
    || !payload.label_key.trim()
    || !payload.name.trim()
  ) {
    throw new ExplorerContractError(`${contract} returned an empty key or name.`);
  }
  if (payload.created !== undefined && typeof payload.created !== 'boolean') {
    throw new ExplorerContractError(`${contract} returned an invalid created flag.`);
  }
  return payload as CreatedLabel;
}

const FORMAT_TOTAL_STATUSES = new Set<ChannelExplorerFormatStatus>([
  'ready',
  'incomplete',
  'stale',
  'not_collected',
]);

function validateFormatSourceCoverage(coverage: Record<string, unknown>, contract: string) {
  for (const field of ['stale_inventory_count', 'availability_withheld_count', 'unavailable_excluded_count']) {
    if (coverage[field] !== undefined && !isNonNegativeInteger(coverage[field])) {
      throw new ExplorerContractError(`${contract} returned invalid ${field} coverage.`);
    }
  }
}

function validateTypedFormatTotal(format: Record<string, unknown>, key: string, contract: string) {
  const path = `formats.${key}`;
  if (format.status === 'absent') {
    validateRequiredFields(format, contract, path, {
      views: (value) => value === null,
      reason: (value) => value === 'playlist_not_found',
      source_expires_at: (value) => utcTimestampMicroseconds(value) !== null,
    });
    return;
  }
  if (format.status === 'not_collected'
      || (format.status === 'incomplete' && format.reason === 'inventory_not_completed')) {
    validateRequiredFields(format, contract, path, { views: (value) => value === null });
    if (format.reason !== undefined && format.reason !== 'inventory_not_completed') {
      throw new ExplorerContractError(`${contract} returned an invalid inventory reason.`);
    }
    return;
  }
  if (format.status === 'stale' && format.reason === 'inventory_stale') {
    validateRequiredFields(format, contract, path, { views: (value) => value === null });
    return;
  }
  validateRequiredFields(format, contract, path, {
    status: (value) => value === 'ready' || value === 'incomplete' || value === 'stale',
    views: (value) => value === null || isNonNegativeIntegerValue(value),
    scanned_inventory_count: isNonNegativeInteger,
    known_view_count: isNonNegativeInteger,
    missing_view_count: isNonNegativeInteger,
  });
  const coverage = requireRecord(format, 'coverage', contract);
  validateRequiredFields(coverage, contract, `${path}.coverage`, {
    scan_limit: isNonNegativeInteger,
    inventory_complete: isBoolean,
    inventory_ready: isTrue,
    product_exposure_ready: isBoolean,
    missing_video_row_count: isNonNegativeInteger,
    unknown_view_count: isNonNegativeInteger,
    stale_counter_count: isNonNegativeInteger,
    stale_metadata_count: isNonNegativeInteger,
    type_resolution_withheld: isNonNegativeInteger,
    stream_completion_withheld: isNonNegativeInteger,
    transactional_snapshot: isFalse,
  });
  validateFormatSourceCoverage(coverage, contract);
  const scanned = Number(format.scanned_inventory_count);
  const known = Number(format.known_view_count);
  const missing = Number(format.missing_view_count);
  // Videos confirmed as no longer public are left out of the sum, not missing.
  const unavailable = Number(coverage.unavailable_excluded_count ?? 0);
  const excluded = Number(coverage.missing_video_row_count)
    + Number(coverage.unknown_view_count)
    + Number(coverage.stale_counter_count)
    + Number(coverage.stale_metadata_count)
    + Number(coverage.type_resolution_withheld)
    + Number(coverage.stream_completion_withheld)
    + Number(coverage.stale_inventory_count ?? 0)
    + Number(coverage.availability_withheld_count ?? 0);
  const ready = format.status === 'ready';
  if (
    scanned > Number(coverage.scan_limit)
    || known + missing + unavailable !== scanned
    || missing !== excluded
    || ready !== (format.views !== null)
    || (ready && (coverage.inventory_complete !== true || missing !== 0))
    || (ready && scanned === 0 && String(format.views) !== '0')
  ) {
    throw new ExplorerContractError(`${contract} returned inconsistent ${key} coverage.`);
  }
}

function validateListInventory(root: Record<string, unknown>, contract: string) {
  if (root.inventory === undefined) return; // Older responses cannot prove a difference.
  const inventory = requireRecord(root, 'inventory', contract);
  validateRequiredFields(inventory, contract, 'inventory', {
    complete: isBoolean,
    unique_video_count: (value) => value === null || isNonNegativeInteger(value),
    youtube_video_count: (value) => value === null || isNonNegativeInteger(value),
    source_expires_at: (value) => value === null || utcTimestampMicroseconds(value) !== null,
  });
  const formats = requireRecord(inventory, 'formats', contract);
  let complete = true;
  let summed = 0;
  let largest = 0;
  for (const key of ['long', 'short', 'stream']) {
    const format = requireRecord(formats, key, contract);
    validateRequiredFields(format, contract, `inventory.formats.${key}`, {
      status: (value) => typeof value === 'string'
        && ['complete', 'absent', 'not_collected', 'incomplete', 'stale'].includes(value),
      listed_video_count: (value) => value === null || isNonNegativeInteger(value),
      source_expires_at: (value) => value === null || utcTimestampMicroseconds(value) !== null,
    });
    const ready = format.status === 'complete' || format.status === 'absent';
    if ((ready && (format.listed_video_count === null || format.source_expires_at === null))
        || (!ready && format.listed_video_count !== null)
        || (format.status === 'absent' && format.listed_video_count !== 0)) {
      throw new ExplorerContractError(`${contract} returned inconsistent list evidence.`);
    }
    complete = complete && ready;
    summed += Number(format.listed_video_count ?? 0);
    largest = Math.max(largest, Number(format.listed_video_count ?? 0));
  }
  if (inventory.complete !== complete
      || (complete && (inventory.unique_video_count === null || inventory.source_expires_at === null
        || Number(inventory.unique_video_count) > summed || Number(inventory.unique_video_count) < largest))
      || (!complete && inventory.unique_video_count !== null)) {
    throw new ExplorerContractError(`${contract} returned inconsistent unique list coverage.`);
  }
}

export function parseExplorerFormatTotalsV1(
  payload: unknown,
  expectedChannelId?: string,
): ChannelExplorerFormatTotalsPayload {
  const contract = 'Explorer format totals';
  const root = requireVersion(payload, contract);
  validateRequiredFields(root, contract, 'payload', {
    channel_id: isText,
    generated_at: (value) => utcTimestampMicroseconds(value) !== null,
  });
  if (expectedChannelId && root.channel_id !== expectedChannelId) {
    throw new ExplorerContractError(`${contract} returned a different channel than requested.`);
  }

  const formats = requireRecord(root, 'formats', contract);
  const long = requireRecord(formats, 'long', contract);
  validateRequiredFields(long, contract, 'formats.long', {
    status: (value) => typeof value === 'string'
      && FORMAT_TOTAL_STATUSES.has(value as ChannelExplorerFormatStatus)
      && value !== 'not_collected',
    views: (value) => value === null || isNonNegativeIntegerValue(value),
    scanned_inventory_count: isNonNegativeInteger,
    known_view_count: isNonNegativeInteger,
    missing_view_count: isNonNegativeInteger,
  });
  for (const key of ['short', 'stream'] as const) {
    const format = requireRecord(formats, key, contract);
    validateTypedFormatTotal(format, key, contract);
  }

  const coverage = requireRecord(root, 'coverage', contract);
  validateRequiredFields(coverage, contract, 'coverage', {
    scan_limit: isNonNegativeInteger,
    inventory_complete: isBoolean,
    missing_video_row_count: isNonNegativeInteger,
    unknown_view_count: isNonNegativeInteger,
    stale_counter_count: isNonNegativeInteger,
    transactional_snapshot: isFalse,
  });
  validateFormatSourceCoverage(coverage, contract);
  const scannedCount = Number(long.scanned_inventory_count);
  const knownCount = Number(long.known_view_count);
  const missingCount = Number(long.missing_view_count);
  const unavailableCount = Number(coverage.unavailable_excluded_count ?? 0);
  const categorizedMissingCount = Number(coverage.missing_video_row_count)
    + Number(coverage.unknown_view_count)
    + Number(coverage.stale_counter_count)
    + Number(coverage.stale_inventory_count ?? 0)
    + Number(coverage.availability_withheld_count ?? 0);
  if (
    scannedCount > Number(coverage.scan_limit)
    || knownCount + missingCount + unavailableCount !== scannedCount
    || missingCount !== categorizedMissingCount
  ) {
    throw new ExplorerContractError(`${contract} returned inconsistent coverage counts.`);
  }
  const ready = long.status === 'ready';
  if (
    ready !== (long.views !== null)
    || (ready && (
      coverage.inventory_complete !== true
      || Number(coverage.missing_video_row_count) !== 0
      || Number(coverage.unknown_view_count) !== 0
      || Number(coverage.stale_counter_count) !== 0
      || Number(long.missing_view_count) !== 0
      || Number(long.known_view_count) + unavailableCount !== Number(long.scanned_inventory_count)
    ))
  ) {
    throw new ExplorerContractError(`${contract} returned inconsistent long-form coverage.`);
  }
  validateListInventory(root, contract);
  requireSource(root, contract, 'derived_from_stored_typed_video_inventory', {
    youtube_requests: false,
    database_writes: false,
    snapshots_used: false,
    freshness_enforced: true,
  });
  return root as ChannelExplorerFormatTotalsPayload;
}

const COUNTER_DYNAMICS_STATUSES = new Set<ChannelExplorerCounterDynamicsStatus>([
  'ready',
  'partial',
  'not_reported',
  'not_enough_history',
  'boundary_too_distant',
  'stale_current',
  'zero_baseline',
  'unavailable',
]);

const isCounterDynamicsStatus: ValueGuard = (value) => (
  typeof value === 'string'
  && COUNTER_DYNAMICS_STATUSES.has(value as ChannelExplorerCounterDynamicsStatus)
);
const isNullableUtcTimestamp: ValueGuard = (value) => (
  value === null || utcTimestampMicroseconds(value) !== null
);
const isNullableNonNegativeNumericValue: ValueGuard = (value) => (
  value === null
  || (typeof value === 'number' && Number.isFinite(value) && value >= 0)
  || (
    typeof value === 'string'
    && /^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value)
  )
);
const isNullableFiniteNumericValue: ValueGuard = (value) => (
  value === null
  || (isNonNullNumericValue(value) && Number.isFinite(Number(value)))
);

function isZeroNumericValue(value: unknown): boolean {
  return value === 0 || (typeof value === 'string' && /^0(?:\.0+)?$/.test(value));
}

function validateCounterDynamicsMetric(
  metric: Record<string, unknown>,
  contract: string,
  section: string,
): void {
  validateRequiredFields(metric, contract, section, {
    status: isCounterDynamicsStatus,
    start_value: isNullableNonNegativeNumericValue,
    end_value: isNullableNonNegativeNumericValue,
    absolute_change: isNumericValue,
    percent_change: isNullableFiniteNumericValue,
    observed_start_at: isNullableUtcTimestamp,
    observed_end_at: isNullableUtcTimestamp,
    actual_interval_seconds: isNullableFiniteNumericValue,
    reason: isNullableText,
  });

  if (
    metric.actual_interval_seconds !== null
    && Number(metric.actual_interval_seconds) < 0
  ) {
    throw new ExplorerContractError(
      `${contract} returned an invalid ${section}.actual_interval_seconds value.`,
    );
  }

  const status = metric.status as ChannelExplorerCounterDynamicsStatus;
  const observedStart = utcTimestampMicroseconds(metric.observed_start_at);
  const observedEnd = utcTimestampMicroseconds(metric.observed_end_at);
  if (observedStart !== null && observedEnd !== null && observedEnd < observedStart) {
    throw new ExplorerContractError(
      `${contract} returned an invalid ${section} observation interval.`,
    );
  }

  if (status === 'ready' || status === 'partial') {
    const requiredReadyValues = [
      metric.start_value,
      metric.end_value,
      metric.absolute_change,
      metric.percent_change,
      metric.actual_interval_seconds,
    ];
    if (
      requiredReadyValues.some((value) => value === null)
      || observedStart === null
      || observedEnd === null
      || observedEnd <= observedStart
      || Number(metric.actual_interval_seconds) <= 0
      || isZeroNumericValue(metric.start_value)
      || (status === 'partial' && (
        Number(metric.actual_interval_seconds) !== Number((observedEnd - observedStart) / 1_000_000n)
        || (metric.coverage !== undefined && metric.coverage !== 'partial')
      ))
      || (status === 'ready' ? metric.reason !== null : typeof metric.reason !== 'string' || !metric.reason.trim())
    ) {
      throw new ExplorerContractError(
        `${contract} returned an incomplete ready ${section} result.`,
      );
    }
    return;
  }

  if (
    metric.percent_change !== null
    || typeof metric.reason !== 'string'
    || metric.reason.trim() === ''
  ) {
    throw new ExplorerContractError(
      `${contract} must keep unavailable ${section} percentages null and explain why.`,
    );
  }
  if (status === 'zero_baseline' && !isZeroNumericValue(metric.start_value)) {
    throw new ExplorerContractError(
      `${contract} returned ${section}.zero_baseline without a zero start value.`,
    );
  }
}

export function parseExplorerCounterDynamicsPeriodsV1(
  payload: unknown,
  expectedChannelId?: string,
): ChannelExplorerCounterDynamicsPeriodsPayload {
  const contract = 'Explorer counter dynamics periods';
  const root = requireVersion(payload, contract);
  validateRequiredFields(root, contract, 'payload', {
    channel_id: (value) => typeof value === 'string' && value.trim() !== '',
    generated_at: (value) => utcTimestampMicroseconds(value) !== null,
  });
  if (expectedChannelId && root.channel_id !== expectedChannelId) {
    throw new ExplorerContractError(`${contract} returned a different channel than requested.`);
  }

  const periods = requireRecord(root, 'periods', contract);
  const expectedPeriods = [
    ['7d', 7 * 24 * 60 * 60],
    ['28d', 28 * 24 * 60 * 60],
  ] as const;
  for (const [periodKey, windowSeconds] of expectedPeriods) {
    const period = requireRecord(periods, periodKey, contract);
    validateRequiredFields(period, contract, `periods.${periodKey}`, {
      period: (value) => value === periodKey,
      window_seconds: (value) => value === windowSeconds,
    });
    const metrics = requireRecord(period, 'metrics', contract);
    for (const metricName of ['subscribers', 'views'] as const) {
      const metric = requireRecord(metrics, metricName, contract);
      validateCounterDynamicsMetric(
        metric,
        contract,
        `periods.${periodKey}.metrics.${metricName}`,
      );
    }
  }

  const methods = requireRecord(root, 'methods', contract);
  validateRequiredFields(methods, contract, 'methods', {
    period_semantics: (value) => value === 'actual_intervals_within_rolling_windows',
    boundary: (value) => value === 'earliest_known_metric_at_or_after_window_start',
    boundary_tolerance_seconds: isNonNegativeInteger,
    current: (value) => value === 'latest_stored_channel_snapshot_at_or_before_generated_at',
    current_max_age_seconds: isNonNegativeInteger,
    percent_change: (value) => value === '(end_value-start_value)/start_value*100',
    counter_corrections: (value) => value === 'preserved_as_signed_changes',
    interpolation: (value) => value === 'none',
    timezone: (value) => value === 'UTC',
  });

  requireSource(root, contract, 'derived_from_stored_channel_snapshots', {
    youtube_requests: false,
    database_writes: false,
    snapshots_used: true,
  });
  return root as ChannelExplorerCounterDynamicsPeriodsPayload;
}


const BASELINE_PERIOD_DEFINITIONS = new Map<
  ChannelBaselinePeriodKey,
  { label: string; days: number | null }
>([
  ['7d', { label: '7D', days: 7 }],
  ['28d', { label: '28D', days: 28 }],
  ['3m', { label: '3M', days: 90 }],
  ['1y', { label: '1Y', days: 365 }],
  ['max', { label: 'MAX', days: null }],
]);
export function parseBaselineV1(
  payload: unknown,
  expectedPeriod?: ChannelBaselinePeriodKey,
): ChannelBaselinePayload {
  const root = requireVersion(payload, 'Baseline');
  const contract = 'Baseline';
  if (root.video_type !== 'long' || root.format !== 'long') {
    throw new ExplorerContractError('Baseline returned an unsupported media format.');
  }
  validateOptionalFields(root, contract, 'payload', {
    generated_at: isText,
  });
  validateFilters(root, contract, { pagination: false });

  const period = optionalRecord(root, 'period', contract);
  if (expectedPeriod !== undefined && !period) {
    throw new ExplorerContractError('Baseline did not confirm the requested publication period.');
  }
  if (period) {
    validateRequiredFields(period, contract, 'period', {
      key: isText,
      label: isText,
      kind: isText,
      days: isNumericValue,
      start_at: isNullableText,
      end_at: isText,
    });
    const periodKey = String(period.key) as ChannelBaselinePeriodKey;
    const definition = BASELINE_PERIOD_DEFINITIONS.get(periodKey);
    if (!definition) {
      throw new ExplorerContractError('Baseline returned an unsupported publication period.');
    }
    if (period.label !== definition.label || period.days !== definition.days) {
      throw new ExplorerContractError('Baseline returned incompatible publication-period metadata.');
    }
    const expectedKind = definition.days === null ? 'max' : 'rolling';
    if (period.kind !== expectedKind) {
      throw new ExplorerContractError('Baseline returned incompatible publication-period semantics.');
    }
    if (expectedPeriod !== undefined && periodKey !== expectedPeriod) {
      throw new ExplorerContractError('Baseline returned a different publication period than requested.');
    }

    const generatedAt = utcTimestampMicroseconds(root.generated_at);
    const periodEnd = utcTimestampMicroseconds(period.end_at);
    if (generatedAt === null || periodEnd === null) {
      throw new ExplorerContractError('Baseline returned an invalid UTC publication-period timestamp.');
    }
    if (period.end_at !== root.generated_at) {
      throw new ExplorerContractError('Baseline publication-period end does not match generated_at.');
    }

    if (definition.days === null) {
      if (period.start_at !== null) {
        throw new ExplorerContractError('Baseline returned an invalid MAX publication period.');
      }
    } else {
      const periodStart = utcTimestampMicroseconds(period.start_at);
      if (periodStart === null) {
        throw new ExplorerContractError('Baseline returned an invalid rolling publication-period start.');
      }
      const expectedSpan = BigInt(definition.days) * 86_400_000_000n;
      if (periodEnd - periodStart !== expectedSpan) {
        throw new ExplorerContractError('Baseline returned an inexact rolling publication-period span.');
      }
    }
  }

  const sample = requireRecord(root, 'sample', contract);
  validateOptionalFields(sample, contract, 'sample', {
    scope: isText,
    scan_limit: scanLimitGuard(sample),
    scanned_inventory_count: isNonNegativeInteger,
    inventory_pagination: isText,
    transactional_snapshot: isFalse,
    inventory_complete: isBoolean,
    truncated: isBoolean,
    loaded_video_count: isNonNegativeInteger,
    missing_video_row_count: isNonNegativeInteger,
    withheld_stale_count: isNonNegativeInteger,
    excluded_by_availability_count: isNonNegativeInteger,
    eligible_current_count: isNonNegativeInteger,
    matched_video_count: isNonNegativeInteger,
    period_future_excluded_count: isNonNegativeInteger,
    filtered_out_count: isNonNegativeInteger,
  });
  if (expectedPeriod !== undefined) {
    validateRequiredFields(sample, contract, 'sample', {
      matched_video_count: isNonNegativeInteger,
      period_future_excluded_count: isNonNegativeInteger,
      filtered_out_count: isNonNegativeInteger,
    });
  }

  const statistics = requireRecord(root, 'statistics', contract);
  for (const key of [
    'views',
    'likes',
    'comments',
    'duration_seconds',
    'lifetime_views_per_day',
    'likes_per_1000_views',
    'comments_per_1000_views',
    'title_length_characters',
    'tag_count',
  ]) {
    const summary = optionalRecord(statistics, key, contract);
    if (summary) validateMetricSummary(summary, contract, `statistics.${key}`);
  }

  const cadence = requireRecord(root, 'publication_cadence', contract);
  validateOptionalFields(cadence, contract, 'publication_cadence', {
    known_publication_count: isNonNegativeInteger,
    missing_or_invalid_count: isNonNegativeInteger,
    future_count: isNonNegativeInteger,
    first_published_at: isNullableText,
    last_published_at: isNullableText,
    interval_count: isNonNegativeInteger,
    mean_gap_days: isNumericValue,
    median_gap_days: isNumericValue,
    q1_gap_days: isNumericValue,
    q3_gap_days: isNumericValue,
    min_gap_days: isNumericValue,
    max_gap_days: isNumericValue,
    publications_last_28_days: isNonNegativeInteger,
    publications_per_30_days: isNumericValue,
  });
  if (expectedPeriod !== undefined) {
    validateRequiredFields(cadence, contract, 'publication_cadence', {
      known_publication_count: isNonNegativeInteger,
      missing_or_invalid_count: isNonNegativeInteger,
      future_count: isNonNegativeInteger,
    });
  }

  if (Object.prototype.hasOwnProperty.call(root, 'publication_points')) {
    const publicationPoints = requireArray(root, 'publication_points', contract);
    validateRecordItems(publicationPoints, contract, 'publication_points', (point, section) => {
      validateRequiredFields(point, contract, section, {
        video_id: isText,
        title: isNullableText,
        published_at: isText,
      });
    });
  }

  const publicationCalendar = requireRecord(root, 'publication_calendar', contract);
  validateRequiredFields(publicationCalendar, contract, 'publication_calendar', {
    // The zone calendar days are counted in: UTC or the viewer's own.
    timezone: (value) => typeof value === 'string' && /^(?:UTC|[A-Za-z]+(?:\/[A-Za-z0-9_+-]+){1,2})$/.test(value),
    default_year: isCalendarYear,
  });
  const availableYears = requireArray(publicationCalendar, 'available_years', contract);
  if (!availableYears.every(isCalendarYear) || new Set(availableYears).size !== availableYears.length) {
    throw new ExplorerContractError('Baseline returned invalid publication_calendar.available_years.');
  }
  const normalizedAvailableYears = availableYears as number[];
  if (normalizedAvailableYears.some((year, index) => index > 0 && normalizedAvailableYears[index - 1] <= year)) {
    throw new ExplorerContractError('Baseline returned publication_calendar.available_years out of descending order.');
  }
  const calendarYears = requireArray(publicationCalendar, 'years', contract);
  validateRecordItems(calendarYears, contract, 'publication_calendar.years', (year, section) => {
    validateRequiredFields(year, contract, section, { year: isCalendarYear });
    const days = requireArray(year, 'days', `${contract} ${section}`);
    const seenDates = new Set<string>();
    validateRecordItems(days, contract, `${section}.days`, (day, daySection) => {
      validateRequiredFields(day, contract, daySection, {
        date: isCalendarDate,
        count: (value) => isNonNegativeInteger(value) && Number(value) > 0,
      });
      const date = String(day.date);
      const previousDate = [...seenDates].at(-1);
      if (
        date.slice(0, 4) !== String(year.year)
        || seenDates.has(date)
        || (previousDate !== undefined && previousDate >= date)
      ) {
        throw new ExplorerContractError(`Baseline returned an invalid ${daySection}.date value.`);
      }
      seenDates.add(date);
    });
  });
  const reportedCalendarYears = calendarYears.map((year) => (
    isRecord(year) ? year.year : null
  ));
  if (
    new Set(reportedCalendarYears).size !== reportedCalendarYears.length
    || reportedCalendarYears.length !== normalizedAvailableYears.length
    || reportedCalendarYears.some((year, index) => year !== normalizedAvailableYears[index])
    || !normalizedAvailableYears.includes(Number(publicationCalendar.default_year))
  ) {
    throw new ExplorerContractError('Baseline returned inconsistent publication_calendar year indexes.');
  }
  const generatedYear = Number(new Intl.DateTimeFormat('en-US', { timeZone: String(publicationCalendar.timezone), year: 'numeric' })
    .format(new Date(String(root.generated_at))));
  if (publicationCalendar.default_year !== generatedYear) {
    throw new ExplorerContractError('Baseline publication_calendar.default_year must match generated_at in its time zone.');
  }

  const contentBreakdown = requireRecord(root, 'content_breakdown', contract);
  const breakdownContracts = new Map([
    ['official_categories', ['single_label', 'youtube.video.snippet.categoryId', 'known_rows']],
    ['youtube_topics', ['multi_label', 'youtube.video.topicDetails.topicCategories', 'sample_rows']],
    ['audio_languages', ['single_label', 'youtube.video.snippet.defaultAudioLanguage', 'known_rows']],
    ['metadata_languages', ['single_label', 'youtube.video.snippet.defaultLanguage', 'known_rows']],
    ['captions', ['single_label', 'youtube.video.contentDetails.caption', 'known_rows']],
    ['catalogue_age', ['single_label', 'leti_cook.video_age_from_published_at', 'known_rows']],
  ]);
  for (const [key, [expectedKind, expectedSource, expectedShareBasis]] of breakdownContracts) {
    const section = requireRecord(contentBreakdown, key, `${contract} content_breakdown`);
    validateRequiredFields(section, contract, `content_breakdown.${key}`, {
      kind: (value) => value === expectedKind,
      source: (value) => value === expectedSource
        || (key === 'catalogue_age' && value === 'let_it_cook.video_age_from_published_at'),
      share_basis: (value) => value === expectedShareBasis,
      sample_count: isNonNegativeInteger,
      known_count: isNonNegativeInteger,
      missing_count: isNonNegativeInteger,
    });
    const sampleCount = Number(section.sample_count);
    const knownCount = Number(section.known_count);
    const missingCount = Number(section.missing_count);
    if (
      knownCount + missingCount !== sampleCount
      || sampleCount !== Number(sample.matched_video_count)
    ) {
      throw new ExplorerContractError(`Baseline returned inconsistent content_breakdown.${key} coverage.`);
    }
    if (key === 'youtube_topics') {
      validateRequiredFields(section, contract, `content_breakdown.${key}`, {
        assigned_count: isNonNegativeInteger,
        empty_count: isNonNegativeInteger,
      });
      if (Number(section.assigned_count) + Number(section.empty_count) !== knownCount) {
        throw new ExplorerContractError('Baseline returned inconsistent content_breakdown.youtube_topics assignment coverage.');
      }
    }
    const groups = requireArray(section, 'groups', `${contract} content_breakdown.${key}`);
    const seenGroupKeys = new Set<string>();
    validateRecordItems(groups, contract, `content_breakdown.${key}.groups`, (group, groupSection) => {
      validateRequiredFields(group, contract, groupSection, {
        key: isText,
        label: isText,
        count: isNonNegativeInteger,
        share_percent: (value) => isFiniteNumber(value) && Number(value) >= 0 && Number(value) <= 100,
        median_views: isNumericValue,
        median_likes: isNumericValue,
        median_comments: isNumericValue,
        views_known_count: isNonNegativeInteger,
        likes_known_count: isNonNegativeInteger,
        comments_known_count: isNonNegativeInteger,
      });
      const groupKey = String(group.key);
      const groupCount = Number(group.count);
      const sharePercent = Number(group.share_percent);
      if (seenGroupKeys.has(groupKey)) {
        throw new ExplorerContractError(`Baseline returned duplicate ${groupSection}.key values.`);
      }
      seenGroupKeys.add(groupKey);
      if (
        Number(group.views_known_count) > groupCount
        || Number(group.likes_known_count) > groupCount
        || Number(group.comments_known_count) > groupCount
      ) {
        throw new ExplorerContractError(`Baseline returned inconsistent ${groupSection} metric coverage.`);
      }
      if (key === 'youtube_topics' && groupCount > Number(section.assigned_count)) {
        throw new ExplorerContractError(`Baseline returned inconsistent ${groupSection} topic count.`);
      }
      const shareDenominator = expectedShareBasis === 'sample_rows' ? sampleCount : knownCount;
      const expectedShare = shareDenominator > 0 ? groupCount * 100 / shareDenominator : 0;
      if (Math.abs(sharePercent - expectedShare) > 0.11) {
        throw new ExplorerContractError(`Baseline returned inconsistent ${groupSection}.share_percent.`);
      }
    });
    if (
      expectedKind === 'single_label'
      && groups.reduce<number>((sum, group) => sum + Number(isRecord(group) ? group.count : 0), 0) !== knownCount
    ) {
      throw new ExplorerContractError(`Baseline returned inconsistent content_breakdown.${key} group counts.`);
    }
  }

  const distributions = requireRecord(root, 'distributions', contract);
  const bucketDistributionKeys = [
    'views',
    'likes',
    'comments',
    'lifetime_views_per_day',
    'likes_per_1000_views',
    'comments_per_1000_views',
    'duration_seconds',
    'publication_years',
    'publication_quarters',
    'video_age_days',
    'publication_gap_days',
    'publication_weekdays_utc',
    'publication_hours_utc',
    'title_length_characters',
    'tag_count',
  ] as const;
  for (const key of bucketDistributionKeys) {
    const buckets = requireArray(distributions, key, contract);
    validateRecordItems(buckets, contract, `distributions.${key}`, (bucket, section) => {
      validateRequiredFields(bucket, contract, section, {
        key: isText,
        label: isText,
        count: isNonNegativeInteger,
      });
    });
  }
  const categories = requireArray(distributions, 'categories', contract);
  validateRecordItems(categories, contract, 'distributions.categories', (category, section) => {
    validateRequiredFields(category, contract, section, {
      category_id: isText,
      category_name: isNullableText,
      count: isNonNegativeInteger,
      share_percent: isNumericValue,
    });
  });
  validateOptionalFields(distributions, contract, 'distributions', {
    known_category_count: isNonNegativeInteger,
    unknown_category_count: isNonNegativeInteger,
  });

  const distributionCoverage = requireRecord(root, 'distribution_coverage', contract);
  for (const key of [...bucketDistributionKeys, 'categories'] as const) {
    const coverage = requireRecord(
      distributionCoverage,
      key,
      `${contract} distribution_coverage`,
    );
    validateRequiredFields(coverage, contract, `distribution_coverage.${key}`, {
      known_count: isNonNegativeInteger,
      missing_count: isNonNegativeInteger,
      excluded_count: isNonNegativeInteger,
    });
  }

  const matchedVideoCount = sample.matched_video_count;
  if (isNonNegativeInteger(matchedVideoCount)) {
    const expectedPopulation = Number(matchedVideoCount);
    for (const key of [...bucketDistributionKeys, 'categories'] as const) {
      const coverage = requireRecord(
        distributionCoverage,
        key,
        `${contract} distribution_coverage`,
      );
      const coveragePopulation = (
        Number(coverage.known_count)
        + Number(coverage.missing_count)
        + Number(coverage.excluded_count)
      );
      if (coveragePopulation !== expectedPopulation) {
        throw new ExplorerContractError(
          `Baseline distribution_coverage.${key} does not match sample.matched_video_count.`,
        );
      }
    }
    if (
      isNonNegativeInteger(cadence.known_publication_count)
      && isNonNegativeInteger(cadence.missing_or_invalid_count)
      && isNonNegativeInteger(cadence.future_count)
      && (
        Number(cadence.known_publication_count)
        + Number(cadence.missing_or_invalid_count)
        + Number(cadence.future_count)
      ) !== expectedPopulation
    ) {
      throw new ExplorerContractError(
        'Baseline publication_cadence does not match sample.matched_video_count.',
      );
    }
  }

  if (
    isNonNegativeInteger(sample.period_future_excluded_count)
    && isNonNegativeInteger(sample.filtered_out_count)
    && Number(sample.period_future_excluded_count) > Number(sample.filtered_out_count)
  ) {
    throw new ExplorerContractError(
      'Baseline period_future_excluded_count exceeds sample.filtered_out_count.',
    );
  }

  const freshness = requireRecord(root, 'freshness', contract);
  validateOptionalFields(freshness, contract, 'freshness', {
    maximum_age_days: isNonNegativeInteger,
    category_cache_available: isBoolean,
    category_cache_fresh: isBoolean,
  });
  for (const key of ['counters', 'metadata']) {
    const range = optionalRecord(freshness, key, contract);
    if (range) validateFreshnessRange(range, contract, `freshness.${key}`);
  }

  const methods = requireRecord(root, 'methods', contract);
  validateOptionalFields(methods, contract, 'methods', {
    nulls: isText,
    quartiles: isText,
    date_filter: isText,
    counter_totals: isText,
    catalogue_filters_applied: isTrue,
    publication_timezone: isText,
    video_age: isText,
    lifetime_views_per_day: isText,
    per_1000_views: isText,
    publication_gaps: isText,
    title_length: isText,
    tag_count: isText,
  });
  const requiredMethods = {
    publication_timezone: 'UTC',
    video_age: 'generated_at_minus_published_at',
    lifetime_views_per_day: 'views_divided_by_exact_checked_at_minus_published_at_minimum_24_hours',
    per_1000_views: 'stored_counter_divided_by_positive_views_times_1000',
    publication_gaps: 'between_consecutive_matched_publications_chronological',
    title_length: 'unicode_code_points',
    tag_count: 'stored_string_list_length_empty_list_is_zero',
  } as const;
  for (const [key, expected] of Object.entries(requiredMethods)) {
    if (methods[key] !== expected) {
      throw new ExplorerContractError(
        `${contract} returned an incompatible methods.${key} invariant.`,
      );
    }
  }
  if (
    expectedPeriod !== undefined
    && methods.period_semantics !== 'published_at_rolling_window_current_counters'
  ) {
    throw new ExplorerContractError(
      `${contract} returned an incompatible methods.period_semantics invariant.`,
    );
  }
  requireSource(root, contract, 'derived_from_stored_current_youtube_fields', {
    format_source: 'video_inventory.video_type',
    youtube_requests: false,
    database_writes: false,
    snapshots_used: false,
    availability_history_used: false,
    trends_used: false,
    performance_scores_used: false,
    freshness_enforced: true,
  });
  // Keep old read payloads compatible without mutating their source clocks or input.
  const catalogueAge = contentBreakdown.catalogue_age as Record<string, unknown>;
  if (catalogueAge.source === 'let_it_cook.video_age_from_published_at') {
    return { ...root, content_breakdown: { ...contentBreakdown,
      catalogue_age: { ...catalogueAge, source: 'leti_cook.video_age_from_published_at' },
    } } as ChannelBaselinePayload;
  }
  return root as ChannelBaselinePayload;
}
