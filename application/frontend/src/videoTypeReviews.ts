import { captureSourceLifetime, transferSourceLifetime } from './sourceLifetime';
import { serverErrorMessage } from './serverText.ts';

/** Operator decisions are app metadata, separate from observed YouTube membership. */
export type VideoReviewType = 'long' | 'short' | 'stream';
export type VideoReviewState = 'open' | 'resolved';
export type VideoReviewFilter = VideoReviewState | 'awaiting' | 'all';
export type VideoReviewStatus = 'needs_review' | 'awaiting_refresh' | 'channel_conflict' | 'confirmed' | 'history';
export type VideoTypeDecision = { video_type: VideoReviewType; reason: string; decided_at: string; revision: number };
export type VideoTypeCase = {
  case_id: string;
  video_id: string | null;
  channel_id: string | null;
  source_detached_at: string | null;
  canonical_type: VideoReviewType | null;
  observed_types: VideoReviewType[];
  state: VideoReviewState;
  review_status: VideoReviewStatus;
  revision: number;
  first_seen_at: string;
  last_seen_at: string;
  can_decide: boolean;
  blocked_reason: 'channel_conflict' | 'source_unavailable' | null;
  decision: VideoTypeDecision | null;
};
export type VideoTypeHistory = VideoTypeDecision & { previous_type: VideoReviewType | null; request_id: string };
export type VideoTypeCasesPage = { cases: VideoTypeCase[]; next_cursor: string | null };
export type VideoTypeCaseDetail = { case: VideoTypeCase; history: VideoTypeHistory[]; next_before_revision: number | null };
export type VideoTypeCommand = {
  request_id: string;
  expected_revision: number;
  video_type: VideoReviewType;
  reason: string;
};
export type VideoTypeMutation = {
  case: VideoTypeCase;
  mutation: { request_id: string; replayed: boolean; applied_revision: number };
};

const ROOT = '/system/video-type-reviews';
const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;
const CHANNEL_ID = /^UC[A-Za-z0-9_-]{22}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
export const VIDEO_TYPE_REASON_LIMIT = 1000;
export const VIDEO_REVIEW_LABELS: Record<VideoReviewType, string> = { long: 'Long', short: 'Short', stream: 'Stream' };

export class VideoTypeReviewError extends Error {
  constructor(message: string, public status?: number, public code?: string) {
    super(message.slice(0, 360));
    this.name = 'VideoTypeReviewError';
  }
}
function invalid(message: string): never { throw new VideoTypeReviewError(`Video type review response is incompatible: ${message}`); }
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return invalid('expected an object.');
  return value as Record<string, unknown>;
}
function envelope(value: unknown): Record<string, unknown> {
  const root = record(value);
  if (root.contract_version !== 2) invalid('unsupported contract version.');
  return root;
}
function text(value: unknown): string { if (typeof value !== 'string') return invalid('expected text.'); return value; }
function id(value: unknown, pattern: RegExp): string { const result = text(value); if (!pattern.test(result)) invalid('invalid identity.'); return result; }
function revision(value: unknown): number {
  if (!Number.isSafeInteger(value) || Number(value) < 1) return invalid('invalid revision.');
  return value as number;
}
function timestamp(value: unknown): string {
  const result = text(value);
  if (!/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(result) || !Number.isFinite(Date.parse(result))) invalid('invalid timestamp.');
  return result;
}
function videoType(value: unknown): VideoReviewType {
  if (value !== 'long' && value !== 'short' && value !== 'stream') return invalid('unknown video type.');
  return value;
}
function nullableType(value: unknown): VideoReviewType | null { return value === null ? null : videoType(value); }
export function videoTypeReason(value: string): string {
  const reason = value.trim();
  if (!reason || [...reason].length > VIDEO_TYPE_REASON_LIMIT || /[\u0000-\u001f\u007f]/.test(reason)
    || [...reason].some((character) => { const point = character.codePointAt(0)!; return point >= 0xd800 && point <= 0xdfff; })) {
    throw new VideoTypeReviewError('Enter a reason between 1 and 1,000 characters, without line breaks or control characters.');
  }
  return reason;
}
function decision(value: unknown): VideoTypeDecision {
  const row = record(value);
  return { video_type: videoType(row.video_type), reason: videoTypeReason(text(row.reason)), decided_at: timestamp(row.decided_at), revision: revision(row.revision) };
}
export function parseVideoTypeCase(value: unknown, expectedId?: string): VideoTypeCase {
  const row = record(value);
  const caseId = id(row.case_id, UUID);
  if (expectedId && caseId !== expectedId) invalid('unexpected review identity.');
  const videoId = row.video_id === null ? null : id(row.video_id, VIDEO_ID);
  const channelId = row.channel_id === null ? null : id(row.channel_id, CHANNEL_ID);
  const detachedAt = row.source_detached_at === null ? null : timestamp(row.source_detached_at);
  const detached = detachedAt !== null;
  if (!Array.isArray(row.observed_types)) invalid('membership evidence is missing.');
  const observed = (row.observed_types as unknown[]).map(videoType);
  if (new Set(observed).size !== observed.length) invalid('duplicate observed types.');
  if (row.state !== 'open' && row.state !== 'resolved') invalid('unknown case state.');
  if (typeof row.can_decide !== 'boolean' || (row.blocked_reason !== null && row.blocked_reason !== 'channel_conflict' && row.blocked_reason !== 'source_unavailable')) invalid('invalid decision eligibility.');
  if (row.can_decide !== (row.blocked_reason === null)) invalid('contradictory decision eligibility.');
  if (detached ? (videoId !== null || channelId !== null || row.canonical_type !== null || observed.length !== 0 || row.blocked_reason !== 'source_unavailable')
    : (videoId === null || channelId === null)) invalid('contradictory source availability.');
  const currentDecision = row.decision === null ? null : decision(row.decision);
  const reviewStatus = row.review_status ?? (currentDecision ? 'history' : 'awaiting_refresh');
  if (!['needs_review', 'awaiting_refresh', 'channel_conflict', 'confirmed', 'history'].includes(String(reviewStatus))) invalid('unknown review status.');
  const currentRevision = revision(row.revision);
  if (row.state === 'resolved' && (!currentDecision || (!detached && (row.canonical_type !== currentDecision.video_type)))) invalid('contradictory resolved case.');
  if (currentDecision && currentDecision.revision > currentRevision) invalid('decision revision is in the future.');
  const firstSeen = timestamp(row.first_seen_at), lastSeen = timestamp(row.last_seen_at);
  if (Date.parse(firstSeen) > Date.parse(lastSeen)) invalid('invalid observation interval.');
  return transferSourceLifetime(row, {
    case_id: caseId, video_id: videoId, channel_id: channelId, source_detached_at: detachedAt, canonical_type: nullableType(row.canonical_type),
    observed_types: observed, state: row.state as VideoReviewState, review_status: reviewStatus as VideoReviewStatus, revision: currentRevision,
    first_seen_at: firstSeen, last_seen_at: lastSeen, can_decide: row.can_decide as boolean,
    blocked_reason: row.blocked_reason as VideoTypeCase['blocked_reason'], decision: currentDecision,
  });
}
export function parseVideoTypeCasesPage(value: unknown, cursor?: string): VideoTypeCasesPage {
  const root = envelope(value);
  if (!Array.isArray(root.cases) || root.cases.length > 50) invalid('invalid cases page.');
  const cases = (root.cases as unknown[]).map((row) => parseVideoTypeCase(row));
  if (new Set(cases.map((row) => row.case_id)).size !== cases.length) invalid('duplicate cases.');
  const next = root.next_cursor === null ? null : id(root.next_cursor, UUID);
  if (next && (cases.length === 0 || next === cursor)) invalid('pagination made no progress.');
  return { cases, next_cursor: next };
}
export function parseVideoTypeCaseDetail(value: unknown, caseId: string, beforeRevision?: number): VideoTypeCaseDetail {
  const root = envelope(value), currentCase = parseVideoTypeCase(root.case, caseId);
  if (!Array.isArray(root.history) || root.history.length > 20) invalid('invalid history page.');
  const history = (root.history as unknown[]).map((value) => {
    const row = record(value);
    return { ...decision(row), previous_type: nullableType(row.previous_type), request_id: id(row.request_id, UUID) };
  });
  if (new Set(history.map((row) => row.revision)).size !== history.length || new Set(history.map((row) => row.request_id)).size !== history.length) invalid('duplicate decision history.');
  if (history.some((row, index) => row.revision > currentCase.revision || (beforeRevision !== undefined && row.revision >= beforeRevision)
    || (index > 0 && row.revision >= history[index - 1].revision))) invalid('history revisions are out of order.');
  const next = root.next_before_revision === null ? null : revision(root.next_before_revision);
  if (next !== null && (!history.length || next !== history[history.length - 1].revision || (beforeRevision !== undefined && next >= beforeRevision))) invalid('history pagination made no progress.');
  return { case: currentCase, history, next_before_revision: next };
}
export function parseVideoTypeMutation(value: unknown, caseId: string, command: VideoTypeCommand): VideoTypeMutation {
  const root = envelope(value), currentCase = parseVideoTypeCase(root.case, caseId), raw = record(root.mutation);
  const requestId = id(raw.request_id, UUID), applied = revision(raw.applied_revision);
  if (requestId !== command.request_id || typeof raw.replayed !== 'boolean' || applied !== command.expected_revision + 1 || applied > currentCase.revision) invalid('decision was not confirmed.');
  if (!raw.replayed && (applied !== currentCase.revision || currentCase.state !== 'resolved')) invalid('decision was not applied to this revision.');
  const current = currentCase.decision;
  if (!current || current.revision < applied || (current.revision === applied && (current.video_type !== command.video_type || current.reason !== command.reason))) invalid('decision acknowledgement does not match the command.');
  return { case: currentCase, mutation: { request_id: requestId, replayed: raw.replayed, applied_revision: applied } };
}

export function newVideoTypeRequestId(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
export function videoTypeReviewLinks(row: Pick<VideoTypeCase, 'video_id' | 'channel_id'>): { video: string | null; channel: string | null } {
  return { video: row.video_id === null ? null : `https://www.youtube.com/watch?v=${encodeURIComponent(id(row.video_id, VIDEO_ID))}`, channel: row.channel_id === null ? null : `https://www.youtube.com/channel/${encodeURIComponent(id(row.channel_id, CHANNEL_ID))}` };
}
async function request(path: string, signal?: AbortSignal, command?: VideoTypeCommand): Promise<unknown> {
  const started = performance.now();
  const controller = new AbortController();
  const cancel = () => controller.abort();
  signal?.addEventListener('abort', cancel, { once: true });
  if (signal?.aborted) controller.abort();
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 30_000);
  try {
    const response = await fetch(path, {
      method: command ? 'POST' : 'GET', cache: 'no-store', signal: controller.signal,
      headers: command ? { Accept: 'application/json', 'Content-Type': 'application/json', 'X-Video-Type-Review': '1' } : { Accept: 'application/json' },
      ...(command ? { body: JSON.stringify(command) } : {}),
    });
    const payload: unknown = await response.json().catch(() => undefined);
    if (!response.ok) {
      const root = payload && typeof payload === 'object' ? payload as Record<string, unknown> : {};
      const detail = root.detail && typeof root.detail === 'object' ? root.detail as Record<string, unknown> : {};
      const code = typeof detail.code === 'string' ? detail.code : undefined;
      throw new VideoTypeReviewError(serverErrorMessage(response.status, code, detail.message), response.status, code);
    }
    return captureSourceLifetime(payload, response.headers, started);
  } catch (error) {
    if (timedOut) throw new VideoTypeReviewError('The request timed out. Its outcome is not confirmed.', 408);
    throw error;
  } finally { clearTimeout(timer); signal?.removeEventListener('abort', cancel); }
}
export async function loadVideoTypeCases(state: VideoReviewFilter, cursor?: string, signal?: AbortSignal): Promise<VideoTypeCasesPage> {
  const query = new URLSearchParams({ state, limit: '50' });
  if (cursor) query.set('cursor', id(cursor, UUID));
  return parseVideoTypeCasesPage(await request(`${ROOT}?${query}`, signal), cursor);
}
export async function loadVideoTypeCase(caseId: string, beforeRevision?: number, signal?: AbortSignal): Promise<VideoTypeCaseDetail> {
  const query = new URLSearchParams({ limit: '20' });
  if (beforeRevision !== undefined) query.set('before_revision', String(revision(beforeRevision)));
  return parseVideoTypeCaseDetail(await request(`${ROOT}/${encodeURIComponent(id(caseId, UUID))}?${query}`, signal), caseId, beforeRevision);
}
export async function decideVideoType(caseId: string, command: VideoTypeCommand): Promise<VideoTypeMutation> {
  id(command.request_id, UUID); revision(command.expected_revision); videoType(command.video_type);
  if (videoTypeReason(command.reason) !== command.reason) throw new VideoTypeReviewError('The reason must not start or end with whitespace.');
  return parseVideoTypeMutation(await request(`${ROOT}/${encodeURIComponent(id(caseId, UUID))}/decision`, undefined, command), caseId, command);
}

export type HiddenVideoIdentities = {
  count: number; videos: { video_id: string; channel_id: string }[]; next_cursor: string | null;
};
export function parseHiddenVideoIdentities(value: unknown): HiddenVideoIdentities {
  const root = record(value);
  if (root.contract_version !== 1 || !Number.isSafeInteger(root.count) || Number(root.count) < 0
      || !Array.isArray(root.videos) || root.videos.length > 100) invalid('invalid hidden video list.');
  const videos = (root.videos as unknown[]).map((value) => {
    const row = record(value);
    return { video_id: id(row.video_id, VIDEO_ID), channel_id: id(row.channel_id, CHANNEL_ID) };
  });
  if (new Set(videos.map((row) => row.video_id)).size !== videos.length) invalid('duplicate hidden video.');
  const next_cursor = root.next_cursor === null ? null : id(root.next_cursor, VIDEO_ID);
  if (next_cursor !== null && next_cursor !== videos.at(-1)?.video_id) invalid('invalid hidden video cursor.');
  return { count: Number(root.count), videos, next_cursor };
}
export async function loadHiddenVideoIdentities({ channelId, cursor, countOnly = false, signal }: {
  channelId?: string; cursor?: string; countOnly?: boolean; signal?: AbortSignal;
} = {}): Promise<HiddenVideoIdentities> {
  const query = new URLSearchParams({ limit: countOnly ? '0' : '50' });
  if (channelId !== undefined) query.set('channel_id', id(channelId, CHANNEL_ID));
  if (cursor !== undefined) query.set('cursor', id(cursor, VIDEO_ID));
  return parseHiddenVideoIdentities(await request(`${ROOT}/hidden?${query}`, signal));
}
