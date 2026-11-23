import { useSourceExpiry } from '../sourceLifetimeReact';
import { sourceContainer, sourceExpired } from '../sourceLifetime.ts';
import {
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import type { ChannelBaselinePeriodKey } from '../types';
import {
  isAbortError,
  isContractError,
  requestJson,
} from './api';
import type { CatalogueConditions, CataloguePresetGroup } from './catalogueQuery';
import {
  OVERVIEW_PERIOD_OPTIONS,
  overviewBaselineScopeKey,
} from './filters';
import { formatCount } from './format';
import { formatTimestampLocal } from './localTime';
import { localTimeZone, type TimeBasis } from './timeBasis';
import type { OverviewAvailability, OverviewCalendarStatus } from './ExplorerPanels';
import { HiddenVideoIdentities } from '../components/HiddenVideoIdentities';
import { DeferredModuleError, DelayedLoadingState, deferredModule, useDeferredModule } from './ModuleState';
import type { ResourceState } from './model';
import {
  beginResourceLoad,
  completeResourceLoad,
  failResourceLoad,
  rejectResourceContract,
  resourcePayload,
  stateForPresentationScope,
} from './resourceState';
import type { LibraryShellContext } from './libraryShell';
import { libraryErrorState } from './libraryData/errors';
import {
  createOverviewClient,
  overviewAvailable,
  overviewBucketConditions,
  overviewPayload,
  overviewPresets,
  REJECTIONS,
  type CalendarYearView,
  type OverviewCapabilities,
  type OverviewClient,
  type OverviewPayload,
  type Rejection,
} from './libraryData/overview';

// Overview of the Library page for any video type: publication-period analysis
// (Publishing rhythm, Channel fingerprint, Content breakdown) from the shared
// backend's Overview operations, its period/time controls and the
// sample coverage shown in the channel actions menu.

export type OverviewState = ResourceState<OverviewPayload>;
type CapabilityRead =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'ready'; value: OverviewCapabilities }
  | { kind: 'error'; message: string };
type CalendarYears = { scopeKey: string; years: CalendarYearView[]; loading: number | null; failed: number | null };

const overviewClient = createOverviewClient(requestJson);

/**
 * The Overview components (rhythm, calendar, fingerprint, breakdown) ship in their
 * own chunk. Overview is the channel page's first section, so the chunk is
 * requested as soon as the page script runs and is normally there before the first
 * channel render; until then the section shows its loading state.
 */
export const overviewPanels = deferredModule(() => import('./ExplorerPanels'));
void overviewPanels.start().catch(() => undefined);

/** State and requests of the Overview section (called by the shell). */
export function useLibraryOverview(context: LibraryShellContext, client: OverviewClient = overviewClient) {
  const {
    channelId, videoType, overviewPeriod, timeZone, overviewVisible,
    baselineReloadToken, setAnnouncement,
  } = context;
  const [overviewState, setOverviewState] = useState<OverviewState>({ kind: 'idle' });
  const [capabilityRead, setCapabilityRead] = useState<CapabilityRead>({ kind: 'idle' });
  const [capabilityToken, setCapabilityToken] = useState(0);
  const [calendarYears, setCalendarYears] = useState<CalendarYears>({ scopeKey: '', years: [], loading: null, failed: null });
  const [, repaintSources] = useState(0);
  useSourceExpiry(overviewState, () => repaintSources(value => value + 1), () => { if (overviewVisible) void calculateOverview(); });
  // The scope changes with channel, format, publication period, time zone or an explicit reload.
  const currentOverviewScope = useMemo(
    () => `${overviewBaselineScopeKey(channelId, overviewPeriod)}\ntype:${videoType}\nzone:${timeZone}\nreload:${baselineReloadToken}`,
    [baselineReloadToken, channelId, overviewPeriod, timeZone, videoType],
  );
  const overviewRequestScopeRef = useRef({ key: currentOverviewScope });
  if (overviewRequestScopeRef.current.key !== currentOverviewScope) {
    overviewRequestScopeRef.current = { key: currentOverviewScope };
  }
  const overviewPresentationScopeRef = useRef<object | null>(null);
  const overviewRequestRef = useRef<AbortController | null>(null);
  const overviewAutoStartedRef = useRef('');
  const yearRequestRef = useRef<AbortController | null>(null);
  const capabilityStartedRef = useRef(-1);

  // `/research/capabilities`: the Overview gate (descriptive analytics) and the
  // per-type bucket sets. Read once when an Overview is first shown;
  // Retry (a new token) reads again. An interrupted read starts over later.
  useEffect(() => {
    if (!overviewVisible || capabilityStartedRef.current === capabilityToken) return undefined;
    capabilityStartedRef.current = capabilityToken;
    const controller = new AbortController();
    let settled = false;
    setCapabilityRead({ kind: 'loading' });
    client.capabilities(controller.signal)
      .then((value) => { settled = true; setCapabilityRead({ kind: 'ready', value }); })
      .catch((error) => {
        if (isAbortError(error)) return;
        settled = true;
        setCapabilityRead({ kind: 'error', message: error instanceof Error ? error.message : String(error) });
      });
    return () => {
      if (settled) return;
      controller.abort();
      capabilityStartedRef.current = -1;
    };
  }, [capabilityToken, client, overviewVisible]);
  const availability: OverviewAvailability = capabilityRead.kind === 'ready'
    ? overviewAvailable(capabilityRead.value) ? 'available' : 'unavailable'
    : capabilityRead.kind === 'error' ? 'error' : 'checking';
  const capabilities = capabilityRead.kind === 'ready' ? capabilityRead.value : null;
  const retryCapabilities = () => setCapabilityToken((value) => value + 1);

  useEffect(() => {
    overviewRequestRef.current?.abort();
    yearRequestRef.current?.abort();
    return () => {
      overviewRequestRef.current?.abort();
      yearRequestRef.current?.abort();
    };
  }, [currentOverviewScope]);

  useEffect(() => {
    if (!overviewVisible) {
      overviewRequestRef.current?.abort();
      yearRequestRef.current?.abort();
    }
  }, [overviewVisible]);

  const visibleOverviewState: OverviewState = stateForPresentationScope(
    overviewState,
    overviewPresentationScopeRef.current,
    overviewRequestScopeRef.current,
    'idle',
  );
  const overview = resourcePayload(visibleOverviewState);

  const calculateOverview = async () => {
    if (availability !== 'available') return;
    overviewRequestRef.current?.abort();
    const controller = new AbortController();
    overviewRequestRef.current = controller;
    const requestScope = overviewRequestScopeRef.current;
    overviewPresentationScopeRef.current = requestScope;
    setOverviewState((current) => beginResourceLoad(current, requestScope.key));
    const presets = (group: Parameters<typeof overviewPresets>[2]) => overviewPresets(capabilities, videoType, group);
    try {
      const signal = controller.signal;
      // Four reads of one population; the server queues them on its read slots.
      const [fingerprint, rhythm, calendar, breakdown] = await Promise.all([
        client.fingerprint(channelId, videoType, timeZone, overviewPeriod, presets, signal),
        client.rhythm(channelId, videoType, timeZone, overviewPeriod, signal),
        client.years(channelId, videoType, timeZone, undefined, signal),
        client.breakdown(channelId, videoType, timeZone, overviewPeriod, signal),
      ]);
      if (overviewRequestScopeRef.current !== requestScope) return;
      setOverviewState(completeResourceLoad(requestScope.key, sourceContainer(overviewPayload(fingerprint, rhythm, calendar, breakdown))));
      setCalendarYears({ scopeKey: requestScope.key, years: [], loading: null, failed: null });
      setAnnouncement('Baseline overview calculated.');
    } catch (error) {
      if (isAbortError(error)) {
        if (overviewRequestScopeRef.current === requestScope) {
          overviewAutoStartedRef.current = '';
          setOverviewState((current) => {
            const previous = resourcePayload(current);
            return previous
              ? completeResourceLoad(requestScope.key, previous)
              : { kind: 'idle' };
          });
        }
        return;
      }
      // The other reads of this attempt are no longer needed.
      controller.abort();
      if (overviewRequestScopeRef.current !== requestScope) return;
      const state = libraryErrorState(error, 'overview');
      const message = state.message ?? (error instanceof Error ? error.message : String(error));
      setOverviewState((current) => (
        isContractError(error) || state.action === 'incompatible'
          ? rejectResourceContract(current, requestScope.key, message)
          : failResourceLoad(current, requestScope.key, message)
      ));
    } finally {
      if (overviewRequestRef.current === controller) {
        overviewRequestRef.current = null;
      }
    }
  };

  useEffect(() => {
    if (
      !overviewVisible
      || availability !== 'available'
      || visibleOverviewState.kind !== 'idle'
      || overviewAutoStartedRef.current === currentOverviewScope
    ) return;
    overviewAutoStartedRef.current = currentOverviewScope;
    void calculateOverview();
  }, [availability, currentOverviewScope, overviewVisible, visibleOverviewState.kind]);

  // The calendar year is chosen apart from the period: one read per other year.
  const currentScopeKey = overviewRequestScopeRef.current.key;
  const loadedYears = useMemo(() => (calendarYears.scopeKey === currentScopeKey
    ? calendarYears.years.filter((year) => !sourceExpired(year)) : []), [calendarYears, currentScopeKey]);
  useSourceExpiry(calendarYears.years, () => repaintSources(value => value + 1));
  const requestCalendarYear = (year: number) => {
    const calendar = overview?.publication_calendar;
    if (!calendar || calendar.years.some((item) => item.year === year) || loadedYears.some((item) => item.year === year)) return;
    const scopeKey = overviewRequestScopeRef.current.key;
    yearRequestRef.current?.abort();
    const controller = new AbortController();
    yearRequestRef.current = controller;
    setCalendarYears({ scopeKey, years: loadedYears, loading: year, failed: null });
    client.years(channelId, videoType, timeZone, year, controller.signal)
      .then((view) => {
        if (overviewRequestScopeRef.current.key !== scopeKey) return;
        setCalendarYears((current) => ({
          scopeKey, years: [...(current.scopeKey === scopeKey ? current.years : []).filter((item) => item.year !== year), view],
          loading: null, failed: null,
        }));
      })
      .catch((error) => {
        if (isAbortError(error) || overviewRequestScopeRef.current.key !== scopeKey) return;
        setCalendarYears((current) => ({ ...current, scopeKey, loading: null, failed: year }));
      })
      .finally(() => { if (yearRequestRef.current === controller) yearRequestRef.current = null; });
  };
  const calendar = useMemo(() => {
    const base = overview?.publication_calendar;
    if (!base || !loadedYears.length) return base;
    const extra = loadedYears.filter((item) => item.time_zone === base.timezone && !base.years.some((entry) => entry.year === item.year));
    return { ...base, years: [...base.years, ...extra.map((item) => ({ year: item.year, days: item.days }))] };
  }, [overview, loadedYears]);
  const calendarStatus: OverviewCalendarStatus = calendarYears.scopeKey === currentScopeKey
    ? { loading: calendarYears.loading, failed: calendarYears.failed } : { loading: null, failed: null };

  /** Conditions that open one Overview bucket in Videos: the type's preset within the exact Overview period. */
  const bucketConditions = (field: CataloguePresetGroup, key: string): CatalogueConditions | null => (
    overviewBucketConditions(overview?.period, field, key, overviewPresets(capabilities, videoType, field))
  );

  const busy = overviewVisible
    && (visibleOverviewState.kind === 'loading' || visibleOverviewState.kind === 'updating');

  return {
    visibleOverviewState, overview, availability, retryCapabilities, calculateOverview, bucketConditions, busy,
    calendar, calendarStatus, requestCalendarYear,
  };
}

export type LibraryOverviewModel = ReturnType<typeof useLibraryOverview>;

/** Content of the Overview tab panel. */
export function LibraryOverviewSection({
  overview,
  context,
  onOverviewBucket,
}: {
  overview: LibraryOverviewModel;
  context: LibraryShellContext;
  onOverviewBucket: (field: CataloguePresetGroup, key: string) => void;
}) {
  const { channelId, videoType } = context;
  const {
    visibleOverviewState, overview: payload, availability, retryCapabilities, calculateOverview,
    calendar, calendarStatus, requestCalendarYear,
  } = overview;
  const panels = useDeferredModule(overviewPanels);
  const hiddenIdentities = <HiddenVideoIdentities key={channelId} channelId={channelId} countOnly />;
  if (!panels.module) {
    return (
      <section className="explorer-panel explorer-overview" aria-label="Overview">
        {hiddenIdentities}
        <section className="explorer-overview-baseline" aria-label="Stored channel analysis">
          {/* the Overview chunk is preloaded; its placeholder shows only when it is slow (cold load). */}
          {panels.failed ? <DeferredModuleError name="Overview" /> : <DelayedLoadingState label="Loading channel analysis…" />}
        </section>
      </section>
    );
  }
  const { OverviewPanel } = panels.module;
  return (
          <OverviewPanel
            key={`${channelId}:${videoType}`}
            hiddenIdentities={hiddenIdentities}
            videoType={videoType}
            overviewState={visibleOverviewState}
            availability={availability}
            calendar={calendar}
            calendarStatus={calendarStatus}
            onCalendarYear={requestCalendarYear}
            onOverviewRetry={() => void calculateOverview()}
            onRetryCapabilities={retryCapabilities}
            // A bucket opens the Videos section of the same format with its own presets.
            onOverviewBucket={payload?.period?.end_at && visibleOverviewState.kind === 'ready' ? onOverviewBucket : undefined}
          />
  );
}

/** Publication period and Local/UTC controls shown with the Overview. */
export function OverviewPeriodSelector({
  overview,
  overviewPeriod,
  setOverviewPeriod,
  timeBasis,
  setTimeBasis,
  setAnnouncement,
}: {
  overview: LibraryOverviewModel;
  overviewPeriod: ChannelBaselinePeriodKey;
  setOverviewPeriod: (period: ChannelBaselinePeriodKey) => void;
  timeBasis: TimeBasis;
  setTimeBasis: (basis: TimeBasis) => void;
  setAnnouncement: (message: string) => void;
}) {
  const { visibleOverviewState } = overview;
  return (
        <div
          className="explorer-v2-period-selector"
          aria-busy={visibleOverviewState.kind === 'loading' || visibleOverviewState.kind === 'updating'}
        >
          <span className="explorer-v2-period-label">Period</span>
          <div
            className="explorer-v2-period-options"
            role="group"
            aria-label="Overview publication period"
            aria-describedby="explorer-v2-period-note"
            title="Filters the analysis by video publication date. Channel counters above remain current totals."
          >
            {OVERVIEW_PERIOD_OPTIONS.map((period) => (
              <button
                key={period.key}
                type="button"
                aria-label={period.description}
                aria-pressed={overviewPeriod === period.key}
                className={overviewPeriod === period.key ? 'is-active' : undefined}
                onClick={() => {
                  if (overviewPeriod === period.key) return;
                  setOverviewPeriod(period.key);
                  setAnnouncement(`${period.description} selected. Updating publication-period analysis.`);
                }}
              >
                {period.label}
              </button>
            ))}
          </div>
          <span className="explorer-sr-only" id="explorer-v2-period-note">
            Filters Publishing rhythm, Channel fingerprint and Content breakdown by video publication date.
            The publication calendar has its own independent calendar-year selection. Channel counters above remain current totals.
          </span>
          <span className="explorer-v2-period-label">Time</span>
          <div
            className="explorer-v2-period-options"
            role="group"
            aria-label="Time used for publication hours, weekdays, days and years"
          >
            {(['local', 'utc'] as const).map((basis) => (
              <button
                key={basis}
                type="button"
                aria-pressed={timeBasis === basis}
                className={timeBasis === basis ? 'is-active' : undefined}
                title={basis === 'local' ? `Your time zone: ${localTimeZone()}` : 'Coordinated Universal Time'}
                onClick={() => {
                  if (timeBasis === basis) return;
                  setTimeBasis(basis);
                  setAnnouncement(`${basis === 'local' ? 'Local time' : 'UTC'} selected for publication hours, days and years.`);
                }}
              >
                {basis === 'local' ? 'Local' : 'UTC'}
              </button>
            ))}
          </div>
        </div>
  );
}

const REJECTION_LABELS: Readonly<Record<Rejection, string>> = {
  identity: 'Unconfirmed list membership',
  source_missing: 'Missing rows',
  source_mismatch: 'Rows of another channel or type',
  source_stale: 'Stale list rows',
  source_unavailable: 'Availability exclusions',
  source_unpublished: 'Not yet published',
  stream_incomplete: 'Streams not ended',
};

/** Overview sample coverage inside the channel actions menu (stale values are kept and counted). */
export function OverviewCoverageSummary({ overview }: { overview: LibraryOverviewModel }) {
  const { overview: payload } = overview;
  if (!payload || sourceExpired(payload.coverage)) return null;
  const coverage = payload.coverage;
  const exact = (value: number) => formatCount(value, { compact: false });
  return (
                <section className="explorer-v2-operation-section explorer-v2-coverage-summary" aria-labelledby="explorer-v2-coverage-title">
                  <div className="explorer-v2-operation-heading">
                    <div>
                      <span className="explorer-eyebrow">Overview sample</span>
                      <h2 id="explorer-v2-coverage-title">Coverage</h2>
                    </div>
                    <small>{formatTimestampLocal(payload.generated_at, 'Unknown')}</small>
                  </div>
                  <dl>
                    <div><dt>Matched videos</dt><dd>{exact(coverage.matched)}</dd></div>
                    <div><dt>Scanned inventory</dt><dd>{exact(coverage.scanned)}</dd></div>
                    <div><dt>Values need refresh</dt><dd title="Kept in the sample; their stale values count as unknown.">{exact(coverage.stale_count)}</dd></div>
                    {REJECTIONS.filter((reason) => reason === 'source_missing' || reason === 'source_unavailable' || coverage.rejected[reason] > 0)
                      .map((reason) => <div key={reason}><dt>{REJECTION_LABELS[reason]}</dt><dd>{exact(coverage.rejected[reason])}</dd></div>)}
                    {coverage.publication_unknown_excluded > 0 && (
                      <div><dt>Publication date unknown</dt><dd>{exact(coverage.publication_unknown_excluded)}</dd></div>
                    )}
                  </dl>
                </section>
  );
}
