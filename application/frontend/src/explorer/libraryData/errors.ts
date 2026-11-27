// Shared-backend error codes → Library states. One
// table for every Library read; each surface decides only how to draw the
// state. Pure TypeScript: no React, window or network.
import { ExplorerContractError } from '../contracts.ts';
import { FORMAT_TURNED_OFF_TEXT } from '../formatsOff.ts';

/** Where the failed read came from. */
export const LIBRARY_ERROR_SURFACES = [
  'videos_first', 'videos_more', 'select_all', 'facets', 'inspector', 'overview',
] as const;
export type LibraryErrorSurface = typeof LIBRARY_ERROR_SURFACES[number];

/**
 * What the surface does:
 * - retry_once: repeat the read once (the session expired between build and reply), then `error`;
 * - preparing: "Preparing stored catalogue…", up to PREPARING_RETRIES automatic retries PREPARING_DELAY_MS apart, then `error`;
 * - error: message + Retry (a previous result stays, marked);
 * - expired / expired_changed: "This browsing session expired…" + "Reload catalogue" (changed: also announce "Data or access changed.");
 * - await_session: wait for the new session, then read again (facets);
 * - inactive: the Inspector panel stops and asks to reopen the video;
 * - session_message: "Select all" keeps the selection and shows the message;
 * too_large, unavailable, blocked, channel_unavailable, invalid_field, incompatible, runtime_not_ready: mapped below by operation.
 * - aborted: the caller cancelled; show nothing.
 */
export type LibraryErrorAction =
  | 'retry_once' | 'preparing' | 'error' | 'expired' | 'expired_changed' | 'await_session' | 'inactive'
  | 'session_message' | 'too_large' | 'unavailable' | 'blocked' | 'channel_unavailable' | 'invalid_field'
  | 'incompatible' | 'runtime_not_ready' | 'aborted';

export type LibraryErrorState = {
  action: LibraryErrorAction;
  /** The server code (or `contract` / `client_timeout` / `unknown`). */
  code: string;
  /** Use the mapped message for a recognized error code and the error's own message otherwise. */
  message?: string;
  /** `invalid_query` points at the condition (`path`) when the server names it. */
  path?: string;
};

export const PREPARING_RETRIES = 2;
export const PREPARING_DELAY_MS = 2000;

export const LIBRARY_ERROR_TEXT = {
  sessionExpired: 'This browsing session expired. Loaded videos and selection are kept.',
  accessChanged: 'Data or access changed.',
  selectAllExpired: 'The catalogue session expired. Reload before selecting all matching videos.',
  inspectorInactive: 'The catalogue changed. Reopen this video to load its details again.',
  // One refusal code covers five bounds; the server's `limit` says which. Without it no number is claimed.
  tooLarge: 'This selection is too large to browse at once. Narrow the filters.',
  tooLargeRows: 'This selection is too large to browse (more than 50,000 videos).',
  tooLargeMemory: 'This selection is too large to browse: its data exceeds the server’s memory limit. Narrow the filters.',
  tooLargeTime: 'This selection took too long to prepare. Narrow the filters or try again.',
  tooLargeRequest: 'These conditions are too large or too complex for one read. Remove some conditions or values.',
  tooLargeResponse: 'This result is too large to send at once. Narrow the filters or choose fewer columns.',
  fieldDisabled: 'A field in these filters is turned off in Collection fields. Remove that condition or turn the field on.',
  dependencyDisabled: 'Data needed for these filters is turned off in Collection fields.',
  formatTurnedOff: FORMAT_TURNED_OFF_TEXT,
  facetsUnavailable: 'Group counts are unavailable on this server.',
  overviewUnavailable: 'Baseline is unavailable on this server',
  runtimeNotReady: 'Stored data is temporarily unavailable.',
  preparing: 'Preparing stored catalogue…',
} as const;

/**
 * A list past the server's ordered-list limit (`page.order` without IDs), in plain words: it is shown page by
 * page in its order, never cut, but not held as one list.
 */
export function orderLimitText(total: number, limit: number): string {
  return `This list has ${total.toLocaleString('en')} videos, more than the ${limit.toLocaleString('en')} that Library can keep in one ordered list. `
    + 'Videos still load in this order as you scroll, but a video whose counters change meanwhile can move between pages, '
    + 'and the whole list cannot be selected at once. Narrow the filters to keep the complete list.';
}

type Row = Record<LibraryErrorSurface, LibraryErrorAction>;
const row = (
  videos_first: LibraryErrorAction, videos_more: LibraryErrorAction, select_all: LibraryErrorAction,
  facets: LibraryErrorAction, inspector: LibraryErrorAction, overview: LibraryErrorAction,
): Row => ({ videos_first, videos_more, select_all, facets, inspector, overview });

/** Unsupported operation and status combinations return `error`. */
export const LIBRARY_ERROR_TABLE: Readonly<Record<string, Row>> = {
  cursor_expired: row('retry_once', 'expired', 'session_message', 'await_session', 'inactive', 'error'),
  cursor_context_changed: row('retry_once', 'expired_changed', 'session_message', 'await_session', 'inactive', 'retry_once'),
  query_busy: row('error', 'error', 'session_message', 'error', 'error', 'error'),
  catalogue_preparing: row('preparing', 'preparing', 'preparing', 'preparing', 'preparing', 'preparing'),
  backend_unavailable: row('error', 'error', 'session_message', 'error', 'error', 'error'),
  client_timeout: row('error', 'error', 'session_message', 'error', 'error', 'error'),
  query_budget_exceeded: row('too_large', 'error', 'session_message', 'error', 'error', 'error'),
  policy_denied: row('unavailable', 'error', 'session_message', 'unavailable', 'unavailable', 'unavailable'),
  grant_required: row('unavailable', 'error', 'session_message', 'unavailable', 'unavailable', 'unavailable'),
  field_disabled: row('blocked', 'error', 'session_message', 'error', 'error', 'error'),
  dependency_disabled: row('blocked', 'error', 'session_message', 'error', 'error', 'error'),
  format_turned_off: row('blocked', 'error', 'session_message', 'error', 'error', 'error'),
  source_unavailable: row('channel_unavailable', 'expired', 'session_message', 'error', 'unavailable', 'error'),
  invalid_query: row('invalid_field', 'error', 'session_message', 'error', 'error', 'error'),
  incomplete_source: row('incompatible', 'incompatible', 'session_message', 'incompatible', 'error', 'incompatible'),
  contract: row('incompatible', 'incompatible', 'session_message', 'incompatible', 'error', 'incompatible'),
  hot_runtime_not_ready: row('runtime_not_ready', 'error', 'session_message', 'error', 'error', 'error'),
};
const UNSUPPORTED = LIBRARY_ERROR_TABLE.contract;
const FALLBACK = row('error', 'error', 'session_message', 'error', 'error', 'error');

function errorCode(error: unknown): string {
  if (error instanceof ExplorerContractError) return 'contract';
  const code = error && typeof error === 'object' ? (error as { code?: unknown }).code : undefined;
  return typeof code === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(code) ? code : 'unknown';
}

const TOO_LARGE: Readonly<Record<string, string>> = {
  rows: LIBRARY_ERROR_TEXT.tooLargeRows, memory: LIBRARY_ERROR_TEXT.tooLargeMemory, time: LIBRARY_ERROR_TEXT.tooLargeTime,
  request: LIBRARY_ERROR_TEXT.tooLargeRequest, response: LIBRARY_ERROR_TEXT.tooLargeResponse,
};

/** The bound a `query_budget_exceeded` refusal names (`limit`), if any. */
function budgetLimit(error: unknown): string | undefined {
  const limit = error && typeof error === 'object' ? (error as { limit?: unknown }).limit : undefined;
  return typeof limit === 'string' && Object.hasOwn(TOO_LARGE, limit) ? limit : undefined;
}

const FIELD_OFF: Readonly<Record<string, string>> = {
  field_disabled: LIBRARY_ERROR_TEXT.fieldDisabled, dependency_disabled: LIBRARY_ERROR_TEXT.dependencyDisabled,
  format_turned_off: LIBRARY_ERROR_TEXT.formatTurnedOff,
};

function messageFor(action: LibraryErrorAction, code: string, surface: LibraryErrorSurface, limit?: string): string | undefined {
  if (action === 'expired') return LIBRARY_ERROR_TEXT.sessionExpired;
  if (action === 'expired_changed') return `${LIBRARY_ERROR_TEXT.sessionExpired} ${LIBRARY_ERROR_TEXT.accessChanged}`;
  if (action === 'inactive') return LIBRARY_ERROR_TEXT.inspectorInactive;
  // These refusals carry a code and no text, so every surface names them in fixed words:
  // the error's own message would be "The request was not accepted (HTTP 422).".
  if (code === 'query_budget_exceeded') return limit ? TOO_LARGE[limit] : LIBRARY_ERROR_TEXT.tooLarge;
  if (Object.hasOwn(FIELD_OFF, code)) return FIELD_OFF[code];
  if (action === 'runtime_not_ready') return LIBRARY_ERROR_TEXT.runtimeNotReady;
  if (action === 'preparing') return LIBRARY_ERROR_TEXT.preparing;
  if (action === 'session_message' && (code === 'cursor_expired' || code === 'cursor_context_changed' || code === 'source_unavailable')) {
    return LIBRARY_ERROR_TEXT.selectAllExpired;
  }
  if (action === 'unavailable' && surface === 'facets') return LIBRARY_ERROR_TEXT.facetsUnavailable;
  if (action === 'unavailable' && surface === 'overview') return LIBRARY_ERROR_TEXT.overviewUnavailable;
  return undefined;
}

/** The Library state for one failed read on one surface. */
export function libraryErrorState(error: unknown, surface: LibraryErrorSurface): LibraryErrorState {
  if (error && typeof error === 'object' && (error as { name?: unknown }).name === 'AbortError') {
    return { action: 'aborted', code: 'aborted' };
  }
  const code = errorCode(error);
  const table = LIBRARY_ERROR_TABLE[code] ?? (code.startsWith('unsupported_') ? UNSUPPORTED : FALLBACK);
  const action = table[surface];
  const fixed = messageFor(action, code, surface, code === 'query_budget_exceeded' ? budgetLimit(error) : undefined);
  const own = error instanceof Error ? error.message : undefined;
  const path = code === 'invalid_query' && error && typeof error === 'object'
    && typeof (error as { path?: unknown }).path === 'string' ? (error as { path: string }).path : undefined;
  return { action, code, ...(fixed ?? own ? { message: fixed ?? own } : {}), ...(path ? { path } : {}) };
}
