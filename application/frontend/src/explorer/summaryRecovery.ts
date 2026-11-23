import type { SystemSummary } from '../types';
import type { ExplorerHomeSummary } from './model';

export const homeSummaryIncomplete = (payload: ExplorerHomeSummary) => payload.saved_videos === null;

/** Retry a failed read, never an empty history or a genuine domain warning. */
export function systemSummaryIncomplete(payload: SystemSummary): boolean {
  return payload.recent_runs_read_status === 'unavailable'
    || payload.queue_health === null
    || payload.video_type_reviews?.available === false
    || payload.quota?.read_status === 'unavailable';
}
