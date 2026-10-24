import { sourceExpired } from '../sourceLifetime';
import { useId, useMemo, useState, type KeyboardEvent } from 'react';
import { ChevronDown } from 'lucide-react';
import type {
  ChannelBaselineBucket,
  ChannelBaselineDistributionCoverage,
  ChannelBaselineMetricSummary,
  ChannelBaselinePayload,
  Primitive,
} from '../types';
import { numberValue } from '../format';
import type { CataloguePresetGroup } from './catalogueQuery';
import {
  formatCount,
  formatDecimal,
  formatDuration,
  videoNoun,
} from './format';

type FingerprintBucket = {
  key: string;
  label: string;
  count: Primitive;
};

type FingerprintRow = {
  id: string;
  label: string;
  value: string;
  summary: string;
  buckets: FingerprintBucket[];
  coverage?: ChannelBaselineDistributionCoverage;
  method: string;
};

type FingerprintSummaryMode = 'median' | 'mean';
export type OverviewBucketHandler = (field: CataloguePresetGroup, key: string) => void;
const overviewGroup: Record<string, CataloguePresetGroup> = { views: 'views', duration: 'duration', likes: 'likes', comments: 'comments', 'title-length': 'title_length', tags: 'tag_count' };

const percentFormatter = new Intl.NumberFormat('en', { maximumFractionDigits: 1 });

function hasValue(value: Primitive): boolean {
  return value !== null && value !== undefined && value !== '';
}

function totalCount(items: readonly FingerprintBucket[]): number {
  return items.reduce((sum, item) => sum + Math.max(0, numberValue(item.count)), 0);
}

function dominantBucket(items: readonly FingerprintBucket[]): FingerprintBucket | null {
  return items.reduce<FingerprintBucket | null>((largest, item) => (
    !largest || numberValue(item.count) > numberValue(largest.count) ? item : largest
  ), null);
}

function knownCount(
  items: readonly FingerprintBucket[],
  coverage?: ChannelBaselineDistributionCoverage,
): number {
  return hasValue(coverage?.known_count)
    ? Math.max(0, numberValue(coverage?.known_count))
    : totalCount(items);
}

function dominantSummary(
  items: readonly FingerprintBucket[],
  coverage?: ChannelBaselineDistributionCoverage,
): string {
  const dominant = dominantBucket(items);
  const known = knownCount(items, coverage);
  if (!dominant || known <= 0 || numberValue(dominant.count) <= 0) return 'No known values';
  const share = numberValue(dominant.count) * 100 / known;
  return `${percentFormatter.format(share)}% in ${dominant.label}`;
}

function summaryValue(
  summary: ChannelBaselineMetricSummary | undefined,
  mode: FingerprintSummaryMode,
  formatter: (value: Primitive) => string,
  missing = 'Not available',
): string {
  const value = summary?.[mode];
  return hasValue(value) ? formatter(value) : missing;
}

function asFingerprintBuckets(items?: ChannelBaselineBucket[]): FingerprintBucket[] {
  return (items ?? []).map((item, index) => ({
    key: item.key || item.label || `bucket-${index}`,
    label: item.label || item.key || 'Unlabelled',
    count: item.count,
  }));
}

function buildFingerprintRows(
  payload: ChannelBaselinePayload,
  summaryMode: FingerprintSummaryMode,
): FingerprintRow[] {
  const distributions = payload.distributions;
  const coverage = payload.distribution_coverage;
  const statistics = payload.statistics;
  const titleBuckets = asFingerprintBuckets(distributions?.title_length_characters);
  const tagBuckets = asFingerprintBuckets(distributions?.tag_count);

  return [
    {
      id: 'views',
      label: 'Views',
      value: summaryValue(statistics?.views, summaryMode, (value) => formatCount(value)),
      summary: dominantSummary(asFingerprintBuckets(distributions?.views), coverage?.views),
      buckets: asFingerprintBuckets(distributions?.views),
      coverage: coverage?.views,
      method: 'Current stored lifetime view counters. This is not age-normalized performance.',
    },
    {
      id: 'duration',
      label: 'Duration',
      value: summaryValue(statistics?.duration_seconds, summaryMode, formatDuration),
      summary: dominantSummary(asFingerprintBuckets(distributions?.duration_seconds), coverage?.duration_seconds),
      buckets: asFingerprintBuckets(distributions?.duration_seconds),
      coverage: coverage?.duration_seconds,
      method: 'Stored long-form duration. The selected summary describes this matched catalogue; it is not a recommended length.',
    },
    {
      id: 'likes',
      label: 'Likes',
      value: summaryValue(statistics?.likes, summaryMode, (value) => formatCount(value)),
      summary: dominantSummary(asFingerprintBuckets(distributions?.likes), coverage?.likes),
      buckets: asFingerprintBuckets(distributions?.likes),
      coverage: coverage?.likes,
      method: 'Current stored like counters. Missing counters remain unknown and are not converted to zero.',
    },
    {
      id: 'comments',
      label: 'Comments',
      value: summaryValue(statistics?.comments, summaryMode, (value) => formatCount(value)),
      summary: dominantSummary(asFingerprintBuckets(distributions?.comments), coverage?.comments),
      buckets: asFingerprintBuckets(distributions?.comments),
      coverage: coverage?.comments,
      method: 'Current stored comment counters. Missing counters remain unknown and are not converted to zero.',
    },
    {
      id: 'title-length',
      label: 'Title length',
      value: summaryValue(
        statistics?.title_length_characters,
        summaryMode,
        (value) => `${formatDecimal(value)} characters`,
      ),
      summary: dominantSummary(titleBuckets, coverage?.title_length_characters),
      buckets: titleBuckets,
      coverage: coverage?.title_length_characters,
      method: 'Stored title length measured in Unicode characters.',
    },
    {
      id: 'tags',
      label: 'Tags',
      value: summaryValue(statistics?.tag_count, summaryMode, (value) => formatDecimal(value)),
      summary: dominantSummary(tagBuckets, coverage?.tag_count),
      buckets: tagBuckets,
      coverage: coverage?.tag_count,
      method: 'Count of existing stored tags. Missing tag data remains unknown.',
    },
  ];
}

function CoverageLine({
  coverage,
  fallbackKnown,
}: {
  coverage?: ChannelBaselineDistributionCoverage;
  fallbackKnown: number;
}) {
  const known = hasValue(coverage?.known_count)
    ? Math.max(0, numberValue(coverage?.known_count))
    : fallbackKnown;
  const missing = numberValue(coverage?.missing_count);
  const excluded = numberValue(coverage?.excluded_count);
  return (
    <span className="explorer-fingerprint-coverage">
      {formatCount(known, { compact: false })} known
      {missing > 0 ? ` · ${formatCount(missing, { compact: false })} missing` : ''}
      {excluded > 0 ? ` · ${formatCount(excluded, { compact: false })} excluded` : ''}
    </span>
  );
}

function FingerprintRowView({
  row,
  open,
  onToggle,
  idPrefix,
  onOverviewBucket,
}: {
  row: FingerprintRow;
  open: boolean;
  onToggle: () => void;
  idPrefix: string;
  onOverviewBucket?: OverviewBucketHandler;
}) {
  const known = knownCount(row.buckets, row.coverage);
  const maximum = row.buckets.reduce((value, bucket) => Math.max(value, numberValue(bucket.count)), 0);
  const dominant = dominantBucket(row.buckets);
  return (
    <article className={`explorer-fingerprint-row${open ? ' is-open' : ''}`}>
      <button
        type="button"
        className="explorer-fingerprint-head"
        aria-expanded={open}
        aria-controls={`${idPrefix}-${row.id}`}
        onClick={onToggle}
      >
        <span className="explorer-fingerprint-label">{row.label}</span>
        <strong>{row.value}</strong>
        <span className="explorer-fingerprint-spark" aria-hidden="true">
          {row.buckets.map((bucket) => (
            <i
              key={bucket.key}
              className={bucket.key === dominant?.key ? 'is-dominant' : undefined}
              style={{
                height: maximum > 0 && numberValue(bucket.count) > 0
                  ? `${Math.max(5, numberValue(bucket.count) * 100 / maximum)}%`
                  : '0%',
              }}
            />
          ))}
        </span>
        <span className="explorer-fingerprint-summary">{row.summary}</span>
        <ChevronDown size={15} aria-hidden="true" />
      </button>

      <div
        id={`${idPrefix}-${row.id}`}
        className="explorer-fingerprint-body"
        hidden={!open}
      >
        <div className="explorer-fingerprint-bars" role="list">
          {row.buckets.length ? row.buckets.map((bucket) => {
            const count = Math.max(0, numberValue(bucket.count));
            const share = known > 0 ? count * 100 / known : 0;
            return (
              <div
                key={bucket.key}
                className={bucket.key === dominant?.key && count > 0 ? 'is-dominant' : undefined}
                role="listitem"
                aria-label={`${bucket.label}: ${formatCount(bucket.count, { compact: false })}, ${percentFormatter.format(share)}% of known`}
              >
                <span title={bucket.label}>{bucket.label}</span>
                <i aria-hidden="true"><i style={{ width: `${maximum > 0 ? count * 100 / maximum : 0}%` }} /></i>
                <strong>{count > 0 ? `${formatCount(bucket.count, { compact: false })} · ${percentFormatter.format(share)}%` : 'None'}</strong>
                {onOverviewBucket && overviewGroup[row.id] && (
                  <button type="button" className="catalogue-overview-drilldown"
                    aria-label={`Open Videos: ${row.label} ${bucket.label}, ${count} ${videoNoun(count)}`}
                    onClick={() => onOverviewBucket(overviewGroup[row.id], bucket.key)} />
                )}
              </div>
            );
          }) : <span className="explorer-empty-copy">No distribution data for this sample.</span>}
        </div>
        <p>{row.method}</p>
        <CoverageLine coverage={row.coverage} fallbackKnown={known} />
      </div>
    </article>
  );
}

type FingerprintHeroMetric = {
  id: string;
  noun: string;
  summary?: ChannelBaselineMetricSummary;
  formatter: (value: Primitive) => string;
  known: number | null;
};

function heroMetric(
  id: string,
  noun: string,
  summary: ChannelBaselineMetricSummary | undefined,
  formatter: (value: Primitive) => string,
): FingerprintHeroMetric {
  return {
    id,
    noun,
    summary,
    formatter,
    known: hasValue(summary?.known_count)
      ? Math.max(0, numberValue(summary?.known_count))
      : null,
  };
}

function fingerprintHeroMetrics(payload: ChannelBaselinePayload): FingerprintHeroMetric[] {
  const statistics = payload.statistics;
  return [
    heroMetric('duration', 'duration', statistics?.duration_seconds, formatDuration),
    heroMetric('views', 'views', statistics?.views, formatCount),
    heroMetric('likes', 'likes', statistics?.likes, formatCount),
    heroMetric('comments', 'comments', statistics?.comments, formatCount),
  ];
}

export function ChannelFingerprint({ payload, onOverviewBucket }: { payload: ChannelBaselinePayload; onOverviewBucket?: OverviewBucketHandler }) {
  const [summaryMode, setSummaryMode] = useState<FingerprintSummaryMode>('median');
  const rows = useMemo(
    () => buildFingerprintRows(payload, summaryMode),
    [payload, summaryMode],
  );
  const [openRows, setOpenRows] = useState<Set<string>>(() => new Set());
  const idPrefix = `fingerprint-${useId().replace(/:/g, '')}`;
  const allOpen = rows.length > 0 && rows.every((row) => openRows.has(row.id));
  const totalDurationSeconds = numberValue(payload.statistics?.duration_seconds?.total);
  const matched = payload.sample?.matched_video_count;
  const heroMetrics = fingerprintHeroMetrics(payload);
  const uploads = payload.publication_cadence?.known_publication_count;

  const handleEscape = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Escape' || openRows.size === 0) return;
    event.stopPropagation();
    setOpenRows(new Set());
  };

  if ([payload.statistics, payload.distributions, payload.publication_cadence, payload.sample].some(section => sourceExpired(section))) {
    return <section className="explorer-channel-fingerprint explorer-content-section"><h2>Channel fingerprint</h2><p role="status">Refreshing current data…</p></section>;
  }

  return (
    <section className="explorer-channel-fingerprint explorer-content-section" onKeyDown={handleEscape} aria-labelledby={`${idPrefix}-title`}>
      <header className="explorer-fingerprint-heading explorer-content-heading">
        <h2 className="explorer-eyebrow" id={`${idPrefix}-title`}>Channel fingerprint</h2>
        <p className="explorer-fingerprint-hero-scope">
          {formatCount(matched, { compact: false })} {videoNoun(matched)}
          {totalDurationSeconds > 0 ? ` · ${formatCount(totalDurationSeconds / 3_600, { compact: false })} catalogue hours` : ''}
          {hasValue(payload.statistics?.views?.total) ? ` · ${formatCount(payload.statistics?.views?.total)} current views` : ''}
        </p>
      </header>

      <div className="explorer-fingerprint-tools">
        <div
          className="explorer-fingerprint-summary-switch"
          role="group"
          aria-label="Summary statistic for all fingerprint metrics"
        >
          <button
            type="button"
            className={summaryMode === 'median' ? 'is-active' : undefined}
            aria-pressed={summaryMode === 'median'}
            onClick={() => setSummaryMode('median')}
          >
            Median
          </button>
          <button
            type="button"
            className={summaryMode === 'mean' ? 'is-active' : undefined}
            aria-pressed={summaryMode === 'mean'}
            onClick={() => setSummaryMode('mean')}
          >
            Average
          </button>
        </div>
        <button
          type="button"
          className="explorer-fingerprint-expand-toggle"
          aria-expanded={allOpen}
          aria-controls={idPrefix + '-list'}
          onClick={() => setOpenRows(allOpen ? new Set() : new Set(rows.map((row) => row.id)))}
        >
          {allOpen ? 'Collapse all' : 'Expand all'}
        </button>
      </div>

      <div className="explorer-fingerprint-stat-grid">
        {heroMetrics.map((metric) => {
          const rawValue = metric.summary?.[summaryMode];
          const displayValue = hasValue(rawValue) ? metric.formatter(rawValue) : '—';
          const knownSummary = metric.known === null
            ? 'known sample not reported'
            : `${formatCount(metric.known, { compact: false })} known values`;
          return (
            <article
              key={metric.id}
              className="explorer-fingerprint-stat-card is-static"
              aria-label={
                metric.noun
                + ' '
                + (summaryMode === 'median' ? 'median' : 'average')
                + ': '
                + displayValue
                + '; '
                + knownSummary
              }
            >
              <span className="explorer-fingerprint-stat-label">{summaryMode === 'median' ? 'Median' : 'Average'} {metric.noun}</span>
              <strong className="explorer-fingerprint-stat-value">{displayValue}</strong>
              <small className="explorer-fingerprint-stat-meta">{knownSummary}</small>
            </article>
          );
        })}
        <article className="explorer-fingerprint-stat-card is-static">
          <span className="explorer-fingerprint-stat-label">Uploads</span>
          <strong className="explorer-fingerprint-stat-value">{hasValue(uploads) ? formatCount(Math.round(numberValue(uploads)), { compact: false }) : '—'}</strong>
          <small className="explorer-fingerprint-stat-meta">Published videos</small>
        </article>
      </div>
      <div id={idPrefix + '-list'} className="explorer-fingerprint-list explorer-content-list">
        {rows.map((row) => (
          <FingerprintRowView
            key={row.id}
            row={row}
            open={openRows.has(row.id)}
            idPrefix={idPrefix}
            onOverviewBucket={onOverviewBucket}
            onToggle={() => setOpenRows((current) => {
              const next = new Set(current);
              if (next.has(row.id)) next.delete(row.id);
              else next.add(row.id);
              return next;
            })}
          />
        ))}
      </div>

      <p className="explorer-fingerprint-footnote">
        {summaryMode === 'median' ? 'Median' : 'Average'} summaries and distributions use stored current long-form rows. Unknown values are excluded per metric and are never converted to zero.
      </p>
    </section>
  );
}
