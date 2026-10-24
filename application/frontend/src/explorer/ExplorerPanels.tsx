import { sourceExpired } from '../sourceLifetime';
import {
  AlertTriangle,
  Film,
} from 'lucide-react';
import type {
  ChannelBaselinePayload,
} from '../types';
import { numberValue } from '../format';
import { ChannelFingerprint, type OverviewBucketHandler } from './ChannelFingerprint';
import { OverviewContentBreakdown } from './OverviewContentBreakdown.tsx';
import { HiddenVideoIdentities } from '../components/HiddenVideoIdentities';
import { PublishingRhythm } from './PublishingRhythm.tsx';
import {
  formatCount,
  hasMetricValue,
} from './format';
import type {
  BaselineState,
  CapabilityState,
  ExplorerHydrationPayload,
  ExplorerHydrationState,
  ResourceState,
} from './model';
import {
  hydrationInitialComplete,
  hydrationInventoryReady,
  hydrationPayload,
  hydrationProfileReady,
} from './hydration';
import {
  ErrorState,
  LoadingState,
  UnavailableState,
} from './ModuleState';
import { resourcePayload } from './resourceState';

type OptionalModule = 'baseline' | 'measured_change' | 'timeline';

function optionalAvailability(
  capabilities: CapabilityState,
  module: OptionalModule,
): 'checking' | 'available' | 'unavailable' | 'error' {
  if (capabilities.kind === 'loading') return 'checking';
  if (capabilities.kind === 'error') return 'error';
  return capabilities.payload.channel_explorer[module];
}

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

function OverviewHydrationLoading({
  payload,
}: {
  payload?: ExplorerHydrationPayload;
}) {
  const inventoryReady = payload ? hydrationInventoryReady(payload) : false;
  if (!inventoryReady) {
    return (
      <div className="explorer-hydration-overview is-inventory">
        <div>
          <span>Channel analysis will appear after the channel profile is ready.</span>
        </div>
      </div>
    );
  }
  return (
    <div
      className="explorer-overview-flow explorer-hydration-overview"
      aria-hidden="true"
    >
      <div className="explorer-overview-insights">
        <section className="explorer-hydration-skeleton is-publishing" aria-hidden="true">
          <span className="explorer-eyebrow">Publishing rhythm</span>
          <i className="explorer-hydration-skeleton-line is-short" />
          <i className="explorer-hydration-skeleton-ring" />
          <i className="explorer-hydration-skeleton-line is-button" />
        </section>
        <FingerprintHydrationSkeleton />
      </div>
      <ContentBreakdownHydrationSkeleton />
    </div>
  );
}

function FingerprintHydrationSkeleton() {
  return (
    <section className="explorer-hydration-skeleton is-fingerprint" aria-hidden="true">
      <span className="explorer-eyebrow">Channel fingerprint</span>
      <div className="explorer-hydration-card-grid">
        {Array.from({ length: 5 }, (_, index) => (
          <i className="explorer-hydration-skeleton-card" key={index} />
        ))}
      </div>
      <div className="explorer-hydration-row-list">
        {Array.from({ length: 4 }, (_, index) => (
          <i className="explorer-hydration-skeleton-line" key={index} />
        ))}
      </div>
    </section>
  );
}

function ContentBreakdownHydrationSkeleton() {
  return (
    <section className="explorer-hydration-skeleton is-breakdown" aria-hidden="true">
      <span className="explorer-eyebrow">Content breakdown</span>
      <i className="explorer-hydration-skeleton-line is-heading" />
      <div className="explorer-hydration-breakdown-grid">
        {Array.from({ length: 4 }, (_, index) => (
          <i className="explorer-hydration-skeleton-card" key={index} />
        ))}
      </div>
    </section>
  );
}


function BaselineReady({
  payload,
  currentProfileReady,
  selectionScopeKey,
  onOverviewBucket,
}: {
  payload: ChannelBaselinePayload;
  currentProfileReady: boolean;
  selectionScopeKey: string;
  onOverviewBucket?: OverviewBucketHandler;
}) {
  const sample = sourceExpired(payload.sample) ? undefined : payload.sample;
  const emptySample = hasMetricValue(sample?.matched_video_count)
    && numberValue(sample?.matched_video_count) === 0;
  return (
    <div className="explorer-overview-flow">
      {currentProfileReady && sample?.inventory_complete === false && (
        <div className="explorer-inline-warning" role="status">
          <AlertTriangle size={17} aria-hidden="true" />
          This overview uses a bounded partial inventory scan. Every metric keeps its own coverage below.
        </div>
      )}

      {currentProfileReady && emptySample && (
        <div className="explorer-baseline-empty" role="status">
          <Film size={25} aria-hidden="true" />
          <strong>No eligible long-form videos in the stored sample</strong>
          <span>
            {hasMetricValue(sample?.scanned_inventory_count)
              ? `${formatCount(sample?.scanned_inventory_count, { compact: false })} inventory IDs were scanned; `
              : 'The scanned inventory count was not reported; '}
            unknown, unavailable and stale rows were not converted to zero.
          </span>
        </div>
      )}

      <div className="explorer-overview-insights">
        <PublishingRhythm
          publicationPoints={payload.publication_points}
          publicationCalendar={payload.publication_calendar}
          selectionScopeKey={selectionScopeKey}
        />
        {currentProfileReady
          ? <ChannelFingerprint payload={payload} onOverviewBucket={onOverviewBucket} />
          : <FingerprintHydrationSkeleton />}
      </div>

      {currentProfileReady
        ? <OverviewContentBreakdown payload={payload} />
        : <ContentBreakdownHydrationSkeleton />}

    </div>
  );
}

function BaselineModule({
  state,
  capabilities,
  currentProfileReady,
  onRetry,
  onRetryCapabilities,
  onOverviewBucket,
}: {
  state: BaselineState;
  capabilities: CapabilityState;
  currentProfileReady: boolean;
  onRetry: () => void;
  onRetryCapabilities: () => void;
  onOverviewBucket?: OverviewBucketHandler;
}) {
  const availability = optionalAvailability(capabilities, 'baseline');
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
          currentProfileReady={currentProfileReady}
          selectionScopeKey={state.scopeKey}
          onOverviewBucket={onOverviewBucket}
        />
      )}
    </>
  );
}

export function OverviewPanel({
  channelId,
  baselineState,
  capabilities,
  hydrationState,
  onBaselineRetry,
  onRetryCapabilities,
  onOverviewBucket,
}: {
  channelId: string;
  baselineState: BaselineState;
  capabilities: CapabilityState;
  hydrationState: ExplorerHydrationState;
  onBaselineRetry: () => void;
  onRetryCapabilities: () => void;
  onOverviewBucket?: OverviewBucketHandler;
}) {
  const currentHydration = hydrationPayload(hydrationState);
  const inventoryReady = currentHydration
    ? hydrationInventoryReady(currentHydration)
    : false;
  const currentProfileReady = currentHydration
    ? hydrationProfileReady(currentHydration)
    : false;
  return (
    <section className="explorer-panel explorer-overview" aria-label="Overview">
      <HiddenVideoIdentities key={channelId} channelId={channelId} countOnly />
      <section className="explorer-overview-baseline" aria-label="Stored channel analysis">
        {!currentProfileReady && currentHydration && hydrationInitialComplete(currentHydration) ? (
          <div className="explorer-empty-copy" role="status">No data available.</div>
        ) : !inventoryReady ? (
          <OverviewHydrationLoading payload={currentHydration} />
        ) : (
          <BaselineModule
            state={baselineState}
            capabilities={capabilities}
            currentProfileReady={currentProfileReady}
            onRetry={onBaselineRetry}
            onOverviewBucket={onOverviewBucket}
            onRetryCapabilities={onRetryCapabilities}
          />
        )}
      </section>
    </section>
  );
}
