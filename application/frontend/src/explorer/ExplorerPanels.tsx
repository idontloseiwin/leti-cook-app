import { sourceExpired } from '../sourceLifetime';
import {
  AlertTriangle,
  Film,
} from 'lucide-react';
import type { ReactNode } from 'react';
import type { ChannelBaselinePublicationCalendar, VideoType } from '../types';
import { ChannelFingerprint, type OverviewBucketHandler } from './ChannelFingerprint';
import { OverviewContentBreakdown } from './OverviewContentBreakdown.tsx';
import { PublishingRhythm } from './PublishingRhythm.tsx';
import { formatCount } from './format';
import type { ResourceState } from './model';
import {
  ErrorState,
  LoadingState,
  UnavailableState,
} from './ModuleState';
import { resourcePayload } from './resourceState';
import { OVERVIEW_FORMAT_WORDS, type OverviewPayload } from './libraryData/overview';

/** The Overview gate of `/research/capabilities` (the old `baseline` availability). */
export type OverviewAvailability = 'checking' | 'available' | 'unavailable' | 'error';
/** Another calendar year being read, or the last one that failed. */
export type OverviewCalendarStatus = { loading: number | null; failed: number | null };

function ResourceNotice<T>({
  state,
  title,
  onRetry,
}: {
  state: ResourceState<T>;
  title: string;
  onRetry: () => void;
}) {
  if (state.kind === 'error') {
    return (
      <ErrorState title={`${title} could not load`} message={state.message} onRetry={onRetry}>
        {state.previous && <small>Showing the last successful result for this exact scope.</small>}
      </ErrorState>
    );
  }
  if (state.kind === 'incompatible') {
    return (
      <ErrorState title={`${title} contract is incompatible`} message={state.message} onRetry={onRetry}>
        {state.previous && <small>Showing the last compatible result for this exact scope.</small>}
      </ErrorState>
    );
  }
  if (state.kind === 'updating') {
    return <LoadingState label={`Updating ${title.toLowerCase()}…`} compact />;
  }
  return null;
}

function BaselineReady({
  payload,
  videoType,
  selectionScopeKey,
  onOverviewBucket,
  calendar,
  calendarStatus,
  onCalendarYear,
}: {
  payload: OverviewPayload;
  videoType: VideoType;
  selectionScopeKey: string;
  onOverviewBucket?: OverviewBucketHandler;
  calendar?: ChannelBaselinePublicationCalendar;
  calendarStatus?: OverviewCalendarStatus;
  onCalendarYear?: (year: number) => void;
}) {
  const sample = sourceExpired(payload.sample) ? undefined : payload.sample;
  const emptySample = sample?.matched_video_count === 0;
  return (
    <div className="explorer-overview-flow">
      {sample?.inventory_complete === false && (
        <div className="explorer-inline-warning" role="status">
          <AlertTriangle size={17} aria-hidden="true" />
          This overview uses a bounded partial inventory scan. Every metric keeps its own coverage below.
        </div>
      )}

      {emptySample && (
        <div className="explorer-baseline-empty" role="status">
          <Film size={25} aria-hidden="true" />
          <strong>No eligible {OVERVIEW_FORMAT_WORDS[videoType].plural} in the stored sample</strong>
          <span>
            {`${formatCount(sample?.scanned_inventory_count, { compact: false })} inventory IDs were scanned; `}
            unknown, unavailable and stale rows were not converted to zero.
          </span>
        </div>
      )}

      <div className="explorer-overview-insights">
        <PublishingRhythm
          rhythm={payload.rhythm}
          publicationCalendar={calendar ?? payload.publication_calendar}
          calendarStatus={calendarStatus}
          onCalendarYear={onCalendarYear}
          selectionScopeKey={selectionScopeKey}
        />
        <ChannelFingerprint payload={payload} videoType={videoType} onOverviewBucket={onOverviewBucket} />
      </div>

      <OverviewContentBreakdown payload={payload} videoType={videoType} />

    </div>
  );
}

function BaselineModule({
  state,
  videoType,
  availability,
  onRetry,
  onRetryCapabilities,
  onOverviewBucket,
  calendar,
  calendarStatus,
  onCalendarYear,
}: {
  state: ResourceState<OverviewPayload>;
  videoType: VideoType;
  availability: OverviewAvailability;
  onRetry: () => void;
  onRetryCapabilities: () => void;
  onOverviewBucket?: OverviewBucketHandler;
  calendar?: ChannelBaselinePublicationCalendar;
  calendarStatus?: OverviewCalendarStatus;
  onCalendarYear?: (year: number) => void;
}) {
  if (availability === 'checking') return <LoadingState label="Checking Baseline availability…" compact />;
  if (availability === 'error') {
    return (
      <UnavailableState
        title="Baseline availability could not be confirmed"
        message="Current source data remains available. Optional analysis stays off until the server capability is confirmed."
        onRetryDiscovery={onRetryCapabilities}
      />
    );
  }
  if (availability === 'unavailable') {
    return (
      <UnavailableState
        title="Baseline is unavailable on this server"
        message="The current catalogue remains available and no Baseline request was sent."
      />
    );
  }
  if (state.kind === 'idle') {
    return <LoadingState label="Preparing the stored channel fingerprint…" />;
  }
  if (state.kind === 'loading') return <LoadingState label="Calculating Baseline from stored current rows…" />;
  const payload = resourcePayload(state);
  return (
    <>
      <ResourceNotice state={state} title="Baseline" onRetry={onRetry} />
      {payload && (
        <BaselineReady
          payload={payload}
          videoType={videoType}
          selectionScopeKey={state.scopeKey}
          onOverviewBucket={onOverviewBucket}
          calendar={calendar}
          calendarStatus={calendarStatus}
          onCalendarYear={onCalendarYear}
        />
      )}
    </>
  );
}

export function OverviewPanel({
  hiddenIdentities,
  videoType,
  overviewState,
  availability,
  calendar,
  calendarStatus,
  onCalendarYear,
  onOverviewRetry,
  onRetryCapabilities,
  onOverviewBucket,
}: {
  /** The hidden-video note of the channel (HiddenVideoIdentities, rendered by the section: Health uses it too). */
  hiddenIdentities: ReactNode;
  videoType: VideoType;
  overviewState: ResourceState<OverviewPayload>;
  availability: OverviewAvailability;
  calendar?: ChannelBaselinePublicationCalendar;
  calendarStatus?: OverviewCalendarStatus;
  onCalendarYear?: (year: number) => void;
  onOverviewRetry: () => void;
  onRetryCapabilities: () => void;
  onOverviewBucket?: OverviewBucketHandler;
}) {
  return (
    <section className="explorer-panel explorer-overview" aria-label="Overview">
      {hiddenIdentities}
      <section className="explorer-overview-baseline" aria-label="Stored channel analysis">
        <BaselineModule
          state={overviewState}
          videoType={videoType}
          availability={availability}
          onRetry={onOverviewRetry}
          onOverviewBucket={onOverviewBucket}
          onRetryCapabilities={onRetryCapabilities}
          calendar={calendar}
          calendarStatus={calendarStatus}
          onCalendarYear={onCalendarYear}
        />
      </section>
    </section>
  );
}
