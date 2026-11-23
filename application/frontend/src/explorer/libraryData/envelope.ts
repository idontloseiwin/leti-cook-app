// Shared parts of every frozen Library result: session identity, coverage
// notice and the Inspector's opening context.
// Pure TypeScript: no React, window or network.
import type { LibraryVideoType } from './wire.ts';
import {
  closedObject, contractError, count, openObject, text, utcInstant, instantMilliseconds,
} from './strict.ts';

/**
 * Identity of one frozen session. seals every cursor and frozen_ref with
 * a fresh nonce, so a token never identifies a session; these four values do:
 * all pages of one session repeat them (the metadata is frozen).
 */
export type FrozenSessionIdentity = {
  query_fingerprint: string;
  evaluated_at: string;
  valid_before: string;
  total_count: number;
};

/** The fields of a frozen page (or a frozen-ids reply) the identity is read from. */
export type FrozenSessionSource = {
  query_fingerprint?: unknown;
  context?: { evaluated_at?: unknown; valid_before?: unknown } | unknown;
  page?: { total_count?: unknown } | unknown;
  total_count?: unknown;
};

/** Strictly read the identity of a frozen page or of a frozen-ids reply. */
export function frozenSessionIdentity(value: FrozenSessionSource, maximumRows = 50_000): FrozenSessionIdentity {
  const root = openObject(value, 'result');
  const fingerprint = text(root.query_fingerprint, 'result.query_fingerprint', 64);
  if (!/^[a-f0-9]{64}$/.test(fingerprint)) contractError('result.query_fingerprint', 'expected a fingerprint.');
  const context = openObject(root.context, 'result.context');
  const evaluated = utcInstant(context.evaluated_at, 'result.context.evaluated_at');
  const valid = utcInstant(context.valid_before, 'result.context.valid_before');
  if (instantMilliseconds(valid) <= instantMilliseconds(evaluated)) {
    contractError('result.context.valid_before', 'the session must end after it was evaluated.');
  }
  // A page carries the total in `page`; the frozen-ids reply carries it at the top level.
  const total = root.page !== undefined
    ? count(openObject(root.page, 'result.page').total_count, 'result.page.total_count', maximumRows)
    : count(root.total_count, 'result.total_count', maximumRows);
  return { query_fingerprint: fingerprint, evaluated_at: evaluated, valid_before: valid, total_count: total };
}

/** `session_key = query_fingerprint + "\n" + evaluated_at + "\n" + valid_before + "\n" + total_count`. */
export function frozenSessionKey(identity: FrozenSessionIdentity): string {
  return `${identity.query_fingerprint}\n${identity.evaluated_at}\n${identity.valid_before}\n${identity.total_count}`;
}

/** Next/previous pages and the id export must belong to the session of the first page. */
export function requireSameFrozenSession(first: FrozenSessionIdentity, next: FrozenSessionIdentity): void {
  if (frozenSessionKey(first) !== frozenSessionKey(next)) {
    contractError('result', 'the page belongs to another browsing session.');
  }
}

/** Known coverage reasons; the dictionary stays open. */
const WITHHELD_REASONS = ['source_rows_unavailable', 'source_rows_expiring'] as const;
const STALE_VALUE_REASON = 'rows_with_expired_values';
const INVENTORY_REASON = 'inventory_incomplete';

/** What `ScopeNotice` shows for one result. */
export type CoverageNotice = {
  partial: boolean;
  withheldRows: number;
  staleValueRows: number;
  inventoryIncomplete: number;
  /** Reasons this Library does not name yet: shown as "N other rows excluded (code)", never dropped. */
  other: Array<{ code: string; count: number }>;
};

export function coverageNotice(value: unknown): CoverageNotice {
  const coverage = openObject(value, 'coverage');
  const status = coverage.status;
  if (status !== 'complete' && status !== 'partial') contractError('coverage.status', 'expected complete or partial.');
  const reasons = openObject(coverage.reasons ?? {}, 'coverage.reasons');
  const read = (code: string) => (Object.hasOwn(reasons, code) ? count(reasons[code], `coverage.reasons.${code}`) : 0);
  const named = new Set<string>([...WITHHELD_REASONS, STALE_VALUE_REASON, INVENTORY_REASON]);
  const other = Object.keys(reasons).filter((code) => !named.has(code)).sort()
    .map((code) => {
      if (!/^[a-z][a-z0-9_]{0,63}$/.test(code)) contractError(`coverage.reasons.${code}`, 'unexpected reason code.');
      return { code, count: read(code) };
    })
    .filter((item) => item.count > 0);
  return {
    partial: status === 'partial',
    withheldRows: WITHHELD_REASONS.reduce((sum, code) => sum + read(code), 0),
    staleValueRows: read(STALE_VALUE_REASON),
    inventoryIncomplete: read(INVENTORY_REASON),
    other,
  };
}

/** Frozen page position (only frozen pages carry `previous_cursor`, `start_index`, `total_count`, `anchor_id`). */
export type FrozenPagePosition = {
  hasMore: boolean;
  nextCursor: string | null;
  previousCursor: string | null;
  startIndex: number;
  totalCount: number;
  anchorId: string | null;
};

export function frozenPagePosition(value: unknown, rowCount: number, maximumRows = 50_000): FrozenPagePosition {
  const page = closedObject(value, 'page', ['has_more', 'next_cursor', 'previous_cursor', 'start_index', 'total_count', 'anchor_id']);
  const cursor = (item: unknown, path: string) => (item === null ? null : text(item, path, 4096));
  const position: FrozenPagePosition = {
    hasMore: page.has_more === true ? true : page.has_more === false ? false : contractError('page.has_more', 'expected true or false.'),
    nextCursor: cursor(page.next_cursor, 'page.next_cursor'),
    previousCursor: cursor(page.previous_cursor, 'page.previous_cursor'),
    startIndex: count(page.start_index, 'page.start_index', maximumRows),
    totalCount: count(page.total_count, 'page.total_count', maximumRows),
    anchorId: page.anchor_id === null ? null : text(page.anchor_id, 'page.anchor_id', 64),
  };
  if (position.hasMore !== (position.nextCursor !== null)) contractError('page', 'has_more and next_cursor disagree.');
  if ((position.startIndex > 0) !== (position.previousCursor !== null)) contractError('page', 'start_index and previous_cursor disagree.');
  if (position.startIndex + rowCount > position.totalCount) contractError('page', 'the total is smaller than the rows shown.');
  if (!position.hasMore && position.startIndex + rowCount !== position.totalCount) contractError('page', 'the last page does not reach the total.');
  return position;
}

/**
 * Opening context of the Inspector panels (replaces `browseSession`).
 * `active`: the Inspector was opened in this session and it has not expired.
 */
export type InspectorContext = {
  channelId: string;
  videoType: LibraryVideoType;
  frozenRef: string;
  sessionKey: string;
  checkedAt: string | null;
  active: boolean;
};
