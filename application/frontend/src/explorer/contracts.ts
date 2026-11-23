import type {
  ExplorerChannelLookupResult,
  ChannelExplorerListInventoryPayload,
  ChannelExplorerVideo,
  ChannelExplorerProfilePayload,
  CreatedFolder,
  CreatedLabel,
  ExplorerLabel,
  Folder,
  SidebarData,
  SystemSummary,
} from '../types';
import type {
  ExplorerAddChannelResult,
  ExplorerHomeSummary,
  ExplorerHydrationPayload,
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
const isNumericValue: ValueGuard = (value) => (
  value === null
  || isFiniteNumber(value)
  || (
    typeof value === 'string'
    && /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value)
  )
);
const isStoredDuration: ValueGuard = (value) => (
  value === null || typeof value === 'string' || isFiniteNumber(value)
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
// Per-type readiness (channel_hydration.with_format_readiness): the union of the
// keys of its four shapes (Long progress, initial-job counts, inventory counts,
// unknown). Only video_type, status and the three gates are read.
const FORMAT_READINESS_KEYS = new Set([
  'video_type', 'scope', 'status', 'phase', 'inventory_status', 'inventory_ready', 'current_profile_ready',
  'initial_check_complete', 'runtime_enabled', 'counts_available', 'progress', 'known_count', 'hydrated_count',
  'unavailable_count', 'awaiting_completion_count', 'pending_count', 'missing_queue_count', 'identity_unresolved_count',
  'failed_count', 'total', 'updated', 'unavailable', 'checked', 'pending', 'errors',
]);
const FORMAT_READINESS_STATUSES = new Set([
  ...EXPLORER_HYDRATION_STATUSES, 'unknown', 'disabled', 'pending_inventory', 'not_available', 'collecting', 'checked',
]);

function validateFormatReadiness(root: Record<string, unknown>, contract: string): void {
  const formats = root.formats;
  if (root.format_readiness_version !== 1 || !isRecord(formats)
    || Object.keys(formats).sort().join(',') !== 'long,short,stream') {
    throw new ExplorerContractError(`${contract} returned an unsupported format readiness.`);
  }
  for (const key of ['long', 'short', 'stream']) {
    const format = formats[key];
    if (
      !isRecord(format)
      || Object.keys(format).some((name) => !FORMAT_READINESS_KEYS.has(name))
      || format.video_type !== key
      || typeof format.status !== 'string' || !FORMAT_READINESS_STATUSES.has(format.status)
      || !isBoolean(format.inventory_ready) || !isBoolean(format.current_profile_ready)
      || !isBoolean(format.initial_check_complete)
      // A ready profile is always a checked, stored list.
      || (format.current_profile_ready && !(format.inventory_ready && format.initial_check_complete))
    ) {
      throw new ExplorerContractError(`${contract} returned an inconsistent ${key} readiness.`);
    }
  }
}

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
  if (root.format_readiness_version !== undefined || root.formats !== undefined) {
    validateFormatReadiness(root, contract);
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
  if (!Object.prototype.hasOwnProperty.call(root, 'last_sync_without_errors_at')
      || !isNullableText(root.last_sync_without_errors_at)) {
    throw new ExplorerContractError(`${contract} returned an invalid last_sync_without_errors_at.`);
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
  const saved = root.saved_videos as Record<string, unknown>;
  const turnedOff = saved.turned_off;
  if (turnedOff !== undefined && (!Array.isArray(turnedOff) || turnedOff.length > 2
      || new Set(turnedOff).size !== turnedOff.length
      || turnedOff.some((kind) => !['long', 'short', 'stream'].includes(kind as string)
        || saved[kind as string] !== 0))) {
    throw new ExplorerContractError(`${contract} returned invalid turned-off formats.`);
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

function validateListInventory(root: Record<string, unknown>, contract: string) {
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

/** The list proof of the header's unlisted note: list counts only. */
export function parseExplorerListInventoryV1(
  payload: unknown,
  expectedChannelId?: string,
): ChannelExplorerListInventoryPayload {
  const contract = 'Explorer list inventory';
  const root = requireVersion(payload, contract);
  validateRequiredFields(root, contract, 'payload', {
    channel_id: isText,
    generated_at: (value) => utcTimestampMicroseconds(value) !== null,
  });
  if (expectedChannelId && root.channel_id !== expectedChannelId) {
    throw new ExplorerContractError(`${contract} returned a different channel than requested.`);
  }
  validateListInventory(root, contract);
  if (root.library !== undefined) {
    const library = requireRecord(root, 'library', contract);
    validateRequiredFields(library, contract, 'library', { status: value => value === 'ready' || value === 'partial' });
    const formats = requireRecord(library, 'formats', contract);
    const timestamp = (value: unknown) => value === null || utcTimestampMicroseconds(value) !== null;
    for (const type of ['long', 'short', 'stream']) {
      const item = requireRecord(formats, type, contract);
      validateRequiredFields(item, contract, `library.formats.${type}`, {
        status: value => typeof value === 'string' && ['complete', 'absent', 'checking', 'check_failed',
          'waiting_for_quota', 'partial', 'not_checked', 'turned_off'].includes(value),
        full_completed_at: timestamp, absent_checked_at: timestamp, source_expires_at: timestamp,
      });
      if (item.manual_check !== undefined) {
        const check = requireRecord(item, 'manual_check', contract);
        const count = (v: unknown) => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0;
        validateRequiredFields(check, contract, 'manual_check', {
          epoch: v => v === null || (count(v) && Number(v)>0), unavailable: v => v === null || typeof v === 'string',
          estimated_general: v => v === null || (count(v) && Number(v)>0), quota_retry_at: timestamp,
          request: v => v === null || (typeof v === 'object' && !Array.isArray(v)),
        });
        if (check.request !== null) {
          const request = requireRecord(check, 'request', contract);
          validateRequiredFields(request, contract, 'manual_check.request', {
            id: v => typeof v === 'string' && /^[0-9a-f]{8}-[0-9a-f-]{27}$/.test(v),
            epoch: v => v === check.epoch, pages: count, general: count,
            wait: v => v === null || typeof v === 'string',
            state: v => typeof v === 'string' && ['queued','running','complete','absent','failed'].includes(v),
          });
          if (request.retry_at !== undefined && !timestamp(request.retry_at)) {
            throw new ExplorerContractError('Invalid list check retry time.');
          }
        }
      }
      if ((item.status === 'complete' && !item.full_completed_at) || (item.status === 'absent' && !item.absent_checked_at)
          || ((item.full_completed_at || item.absent_checked_at) && !item.source_expires_at)
          || (library.status === 'ready' && !['complete', 'absent', 'turned_off'].includes(String(item.status)))) {
        throw new ExplorerContractError(`${contract} returned inconsistent format coverage.`);
      }
    }
  }
  requireSource(root, contract, 'stored_channel_list_inventory', {
    youtube_requests: false,
    database_writes: false,
  });
  return root as ChannelExplorerListInventoryPayload;
}
