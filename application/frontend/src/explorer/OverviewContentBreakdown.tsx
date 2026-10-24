import { sourceExpired } from '../sourceLifetime';
import { useEffect, useRef, type KeyboardEvent, type ReactNode } from 'react';
import type {
  ChannelBaselineBreakdownGroup,
  ChannelBaselineBreakdownSection,
  ChannelBaselinePayload,
  Primitive,
} from '../types';
import { numberValue } from '../format';
import { formatCount, videoNoun } from './format';
import {
  formatLanguageDisplayName,
  formatOfficialCategoryLabel,
} from './contentBreakdown';

type BreakdownTone = 'blue' | 'amber';

const percentFormatter = new Intl.NumberFormat('en', { maximumFractionDigits: 1 });

function percent(value: Primitive): number {
  return Math.min(100, Math.max(0, numberValue(value)));
}

function sortedGroups(section: ChannelBaselineBreakdownSection): ChannelBaselineBreakdownGroup[] {
  return [...section.groups].sort((left, right) => right.count - left.count);
}

const catalogueAgeOrder = new Map([
  ['5y_plus', 0],
  ['3_5y', 1],
  ['1_3y', 2],
  ['under_1y', 3],
]);

function orderedCatalogueAgeGroups(
  section: ChannelBaselineBreakdownSection,
): ChannelBaselineBreakdownGroup[] {
  return [...section.groups].sort((left, right) => {
    const leftOrder = catalogueAgeOrder.get(left.key) ?? Number.MAX_SAFE_INTEGER;
    const rightOrder = catalogueAgeOrder.get(right.key) ?? Number.MAX_SAFE_INTEGER;
    return leftOrder - rightOrder || right.count - left.count;
  });
}

function sectionCoverage(section: ChannelBaselineBreakdownSection): string {
  if (section.kind === 'multi_label') {
    const missing = Math.max(0, numberValue(section.missing_count));
    return `${formatCount(section.assigned_count, { compact: false })} assigned · ${formatCount(section.empty_count, { compact: false })} empty${missing > 0 ? ` · ${formatCount(missing, { compact: false })} not collected` : ''}`;
  }
  return `${formatCount(section.known_count, { compact: false })} known · ${formatCount(section.missing_count, { compact: false })} unknown`;
}

function groupShare(
  section: ChannelBaselineBreakdownSection,
  group: ChannelBaselineBreakdownGroup,
): number {
  const reported = percent(group.share_percent);
  if (reported > 0 || group.count === 0) return reported;
  const denominator = section.share_basis === 'known_rows'
    ? section.known_count
    : section.sample_count;
  return denominator > 0 ? Math.min(100, group.count * 100 / denominator) : 0;
}

function sourceDescription(section: ChannelBaselineBreakdownSection): string {
  switch (section.source) {
    case 'youtube.video.snippet.categoryId':
      return 'Uploader-selected YouTube category.';
    case 'youtube.video.topicDetails.topicCategories':
      return 'YouTube-associated multi-label metadata; shares can overlap.';
    case 'youtube.video.snippet.defaultAudioLanguage':
      return 'Declared default audio language from YouTube metadata.';
    case 'youtube.video.snippet.defaultLanguage':
      return 'Declared metadata language from YouTube.';
    case 'youtube.video.contentDetails.caption':
      return 'Caption availability reported by YouTube content details.';
    case 'leti_cook.video_age_from_published_at':
      return 'Age derived from the stored publication date.';
    default:
      return 'Stored video metadata.';
  }
}

function BreakdownDetailRows({
  section,
  formatLabel,
  title = 'All values',
  groups: suppliedGroups,
}: {
  section: ChannelBaselineBreakdownSection;
  formatLabel?: (group: ChannelBaselineBreakdownGroup) => string;
  title?: string;
  groups?: ChannelBaselineBreakdownGroup[];
}) {
  const groups = suppliedGroups ?? sortedGroups(section);
  const empty = Math.max(0, numberValue(section.empty_count));
  const missing = Math.max(0, section.missing_count);
  const sample = Math.max(0, section.sample_count);
  if (!groups.length && empty === 0 && missing === 0) return null;
  return (
    <section className="overview-breakdown-detail-groups">
      <h4>{title}</h4>
      <ul>
        {groups.map((group) => (
          <li key={group.key}>
            <span>{formatLabel ? formatLabel(group) : group.label}</span>
            <strong>
              {formatCount(group.count, { compact: false })} · {percentFormatter.format(groupShare(section, group))}%
            </strong>
          </li>
        ))}
        {empty > 0 && (
          <li>
            <span>No assigned value</span>
            <strong>{formatCount(empty, { compact: false })} · {percentFormatter.format(sample > 0 ? empty * 100 / sample : 0)}%</strong>
          </li>
        )}
        {missing > 0 && (
          <li>
            <span>Unknown</span>
            <strong>{formatCount(missing, { compact: false })} · {percentFormatter.format(sample > 0 ? missing * 100 / sample : 0)}%</strong>
          </li>
        )}
      </ul>
    </section>
  );
}

function BreakdownDetails({
  section,
  formatLabel,
  groups,
  children,
}: {
  section: ChannelBaselineBreakdownSection;
  formatLabel?: (group: ChannelBaselineBreakdownGroup) => string;
  groups?: ChannelBaselineBreakdownGroup[];
  children?: ReactNode;
}) {
  const detailsRef = useRef<HTMLDetailsElement>(null);

  useEffect(() => {
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const details = detailsRef.current;
      if (!details?.open || !(event.target instanceof Node) || details.contains(event.target)) return;
      details.open = false;
    };
    document.addEventListener('pointerdown', closeOnOutsidePointer);
    return () => document.removeEventListener('pointerdown', closeOnOutsidePointer);
  }, []);

  const closeOnEscape = (event: KeyboardEvent<HTMLDetailsElement>) => {
    const details = detailsRef.current;
    if (event.key !== 'Escape' || !details?.open) return;
    event.preventDefault();
    event.stopPropagation();
    details.open = false;
    details.querySelector<HTMLElement>('summary')?.focus();
  };

  return (
    <details ref={detailsRef} className="overview-breakdown-details" onKeyDown={closeOnEscape}>
      <summary>Details</summary>
      <div>
        <p>{sourceDescription(section)}</p>
        <dl>
          <div><dt>Sample</dt><dd>{formatCount(section.sample_count, { compact: false })}</dd></div>
          <div><dt>Known</dt><dd>{formatCount(section.known_count, { compact: false })}</dd></div>
          <div><dt>Unknown</dt><dd>{formatCount(section.missing_count, { compact: false })}</dd></div>
        </dl>
        <BreakdownDetailRows section={section} formatLabel={formatLabel} groups={groups} />
        {children}
      </div>
    </details>
  );
}

function BreakdownBar({
  label,
  count,
  share,
  width,
  tone,
  state = 'known',
}: {
  label: string;
  count: number;
  share: number;
  width: number;
  tone: BreakdownTone;
  state?: 'known' | 'empty' | 'unknown';
}) {
  const normalizedWidth = count > 0 ? Math.max(1.5, Math.min(100, width)) : 0;
  return (
    <div className={`overview-breakdown-bar is-${tone} is-${state}`}>
      <div>
        <span title={label}>{label}</span>
        <strong>{formatCount(count, { compact: false })} · {percentFormatter.format(share)}%</strong>
      </div>
      <i aria-hidden="true"><i style={{ width: `${normalizedWidth}%` }} /></i>
    </div>
  );
}

function GroupBars({
  section,
  tone,
  limit,
  formatLabel,
  relativeToLeader = false,
  groups: suppliedGroups,
}: {
  section: ChannelBaselineBreakdownSection;
  tone: BreakdownTone;
  limit: number;
  formatLabel?: (group: ChannelBaselineBreakdownGroup) => string;
  relativeToLeader?: boolean;
  groups?: ChannelBaselineBreakdownGroup[];
}) {
  const groups = (suppliedGroups ?? sortedGroups(section)).slice(0, limit);
  const maximum = Math.max(0, ...groups.map((group) => group.count));
  if (!groups.length) {
    return <p className="overview-breakdown-empty">No known values in this period.</p>;
  }
  return (
    <div className="overview-breakdown-bars">
      {groups.map((group) => {
        const share = groupShare(section, group);
        return (
          <BreakdownBar
            key={group.key}
            label={formatLabel ? formatLabel(group) : group.label}
            count={group.count}
            share={share}
            width={relativeToLeader && maximum > 0 ? group.count * 100 / maximum : share}
            tone={tone}
          />
        );
      })}
    </div>
  );
}

function OfficialCategories({ section }: { section: ChannelBaselineBreakdownSection }) {
  const groups = sortedGroups(section);
  const dominant = groups[0];
  const dominantShare = dominant ? groupShare(section, dominant) : 0;
  const maximum = Math.max(0, ...groups.map((group) => group.count));
  return (
    <article className="overview-breakdown-column is-categories">
      <header>
        <div><h3>Official categories</h3><p>{sectionCoverage(section)} · share of known</p></div>
        <BreakdownDetails
          section={section}
          formatLabel={(group) => formatOfficialCategoryLabel(group.label)}
        />
      </header>
      {dominant ? (
        <>
          <strong className="overview-breakdown-dominant">{formatOfficialCategoryLabel(dominant.label)}</strong>
          <span className="overview-breakdown-dominant-meta">
            {formatCount(dominant.count, { compact: false })} {videoNoun(dominant.count)} · {percentFormatter.format(dominantShare)}% of known sample
          </span>
          <div className="overview-breakdown-dominant-track" aria-hidden="true"><i /></div>
          {groups.length > 1 && (
            <div className="overview-breakdown-bars is-secondary">
              {groups.slice(1, 5).map((group) => {
                const share = groupShare(section, group);
                return (
                  <BreakdownBar
                    key={group.key}
                    label={formatOfficialCategoryLabel(group.label)}
                    count={group.count}
                    share={share}
                    width={maximum > 0 ? group.count * 100 / maximum : 0}
                    tone="blue"
                  />
                );
              })}
            </div>
          )}
          <p className="overview-breakdown-explainer">
            Author-selected YouTube category. {groups.length === 1
              ? 'This channel keeps a single category across the stored catalogue.'
              : 'Shares describe the stored long-form catalogue.'}
          </p>
        </>
      ) : <p className="overview-breakdown-empty">Official category is not reported.</p>}
      {section.missing_count > 0 && (
        <BreakdownBar
          label="Unknown category"
          count={section.missing_count}
          share={section.sample_count > 0 ? section.missing_count * 100 / section.sample_count : 0}
          width={section.sample_count > 0 ? section.missing_count * 100 / section.sample_count : 0}
          tone="blue"
          state="unknown"
        />
      )}
    </article>
  );
}

function YouTubeTopics({ section }: { section: ChannelBaselineBreakdownSection }) {
  const empty = Math.max(0, numberValue(section.empty_count));
  const missing = Math.max(0, section.missing_count);
  const sample = Math.max(0, section.sample_count);
  return (
    <article className="overview-breakdown-column is-topics">
      <header>
        <div><h3>YouTube topics</h3><p>{sectionCoverage(section)} · shares overlap</p></div>
        <BreakdownDetails section={section} />
      </header>
      <GroupBars section={section} tone="blue" limit={7} />
      {empty > 0 && (
        <BreakdownBar
          label="No assigned topic"
          count={empty}
          share={sample > 0 ? empty * 100 / sample : 0}
          width={sample > 0 ? empty * 100 / sample : 0}
          tone="blue"
          state="empty"
        />
      )}
      {missing > 0 && (
        <BreakdownBar
          label="Topic metadata not collected"
          count={missing}
          share={sample > 0 ? missing * 100 / sample : 0}
          width={sample > 0 ? missing * 100 / sample : 0}
          tone="blue"
          state="unknown"
        />
      )}
    </article>
  );
}

function LanguageMetric({
  title,
  section,
  formatLabel,
  coverageText,
}: {
  title: string;
  section: ChannelBaselineBreakdownSection;
  formatLabel?: (group: ChannelBaselineBreakdownGroup) => string;
  coverageText?: string;
}) {
  const groups = sortedGroups(section);
  const dominant = groups[0];
  const dominantShare = dominant ? groupShare(section, dominant) : 0;
  const known = Math.max(0, section.known_count);
  const isCaptions = section.source === 'youtube.video.contentDetails.caption';
  const barGroups = isCaptions
    ? [...groups].sort((left, right) => {
      if (left.key === 'available') return -1;
      if (right.key === 'available') return 1;
      return 0;
    })
    : groups;
  return (
    <section className={`overview-language-metric${isCaptions ? ' is-captions' : ''}`}>
      <span>{title}</span>
      <div>
        <strong>
          {dominant
            ? `${formatLabel ? formatLabel(dominant) : dominant.label} · ${percentFormatter.format(dominantShare)}%`
            : 'Not reported'}
        </strong>
        <small>
          {coverageText ?? sectionCoverage(section)}
        </small>
      </div>
      <i aria-hidden="true">
        {barGroups.filter((group) => group.count > 0).map((group, index) => (
          <i
            key={group.key}
            className={isCaptions
              ? group.key === 'available' ? 'is-caption-available' : 'is-caption-unavailable'
              : `is-known is-tone-${index % 4}`}
            style={{ width: `${known > 0 ? group.count * 100 / known : 0}%` }}
          />
        ))}
      </i>
    </section>
  );
}

function LanguageAccessibility({
  audio,
  metadata,
  captions,
}: {
  audio: ChannelBaselineBreakdownSection;
  metadata: ChannelBaselineBreakdownSection;
  captions: ChannelBaselineBreakdownSection;
}) {
  const captionGroups = sortedGroups(captions);
  const captionsWith = captionGroups.find((group) => group.key === 'available')?.count ?? 0;
  const captionsWithout = captionGroups.find((group) => group.key === 'unavailable')?.count ?? 0;
  const captionCoverage = `${formatCount(captionsWith, { compact: false })} with · ${formatCount(captionsWithout, { compact: false })} without${captions.missing_count > 0 ? ` · ${formatCount(captions.missing_count, { compact: false })} unknown` : ''}`;
  return (
    <article className="overview-breakdown-column is-language">
      <header>
        <div><h3>Language &amp; accessibility</h3><p>Declared audio · metadata · captions</p></div>
        <BreakdownDetails section={captions}>
          <p>Audio: {sectionCoverage(audio)}. Metadata: {sectionCoverage(metadata)}.</p>
          <BreakdownDetailRows
            section={audio}
            title="Audio languages"
            formatLabel={(group) => formatLanguageDisplayName(group.key, group.label)}
          />
          <BreakdownDetailRows
            section={metadata}
            title="Metadata languages"
            formatLabel={(group) => formatLanguageDisplayName(group.key, group.label)}
          />
        </BreakdownDetails>
      </header>
      <div className="overview-language-stack">
        <LanguageMetric
          title="Audio language"
          section={audio}
          formatLabel={(group) => formatLanguageDisplayName(group.key, group.label)}
        />
        <LanguageMetric
          title="Metadata language"
          section={metadata}
          formatLabel={(group) => formatLanguageDisplayName(group.key, group.label)}
        />
        <LanguageMetric title="Captions" section={captions} coverageText={captionCoverage} />
      </div>
    </article>
  );
}

function CatalogueAge({ section }: { section: ChannelBaselineBreakdownSection }) {
  const groups = orderedCatalogueAgeGroups(section);
  return (
    <article className="overview-breakdown-column is-age">
      <header>
        <div><h3>Catalogue age</h3><p>{sectionCoverage(section)} · share of known</p></div>
        <BreakdownDetails section={section} groups={groups} />
      </header>
      <GroupBars
        section={section}
        tone="amber"
        limit={groups.length}
        relativeToLeader
        groups={groups}
      />
      {section.missing_count > 0 && (
        <BreakdownBar
          label="Publication date unknown"
          count={section.missing_count}
          share={section.sample_count > 0 ? section.missing_count * 100 / section.sample_count : 0}
          width={section.sample_count > 0 ? section.missing_count * 100 / section.sample_count : 0}
          tone="amber"
          state="unknown"
        />
      )}
      <p className="overview-breakdown-explainer">
        Age of stored long-form videos relative to the current data check.
      </p>
    </article>
  );
}

export function OverviewContentBreakdown({ payload }: { payload: ChannelBaselinePayload }) {
  const breakdown = payload.content_breakdown;
  const periodLabel = payload.period?.key?.toUpperCase() ?? 'SELECTED';
  return (
    <section className="overview-breakdown explorer-content-section" aria-labelledby="overview-content-breakdown-title">
      <header className="overview-breakdown-heading">
        <h2 className="explorer-eyebrow" id="overview-content-breakdown-title">Content breakdown</h2>
        {!sourceExpired(payload.sample) && <span>{formatCount(payload.sample?.matched_video_count, { compact: false })} {videoNoun(payload.sample?.matched_video_count)} · {periodLabel} · long-form · one stored sample</span>}
      </header>
      <div className="overview-breakdown-grid">
        {!sourceExpired(breakdown.official_categories) && <OfficialCategories section={breakdown.official_categories} />}
        {!sourceExpired(breakdown.youtube_topics) && <YouTubeTopics section={breakdown.youtube_topics} />}
        {![breakdown.audio_languages, breakdown.metadata_languages, breakdown.captions].some(section => sourceExpired(section)) && <LanguageAccessibility
          audio={breakdown.audio_languages}
          metadata={breakdown.metadata_languages}
          captions={breakdown.captions}
        />}
        {!sourceExpired(breakdown.catalogue_age) && <CatalogueAge section={breakdown.catalogue_age} />}
      </div>
    </section>
  );
}
