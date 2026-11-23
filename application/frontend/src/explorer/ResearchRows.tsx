import { researchTypeNames,researchLabels,safeImage,type ResearchType,type ResearchCell,type ResearchField,type ResearchRow,type ResearchPage } from './researchQuery';
import { GROWTH_STATUS_LABELS, averageViewsPerDay, observedInterval, signedCompactCount } from './videoGrowth';
export function displayResearchCell(cell: ResearchCell | undefined, field: ResearchField): string {
  if (!cell || cell.state !== 'known' || cell.value === null) return cell?.state === 'expired' ? 'Expired' : cell?.state === 'unavailable' ? 'Unavailable' : 'Unknown';
  if (typeof cell.value === 'boolean') return cell.value ? 'Yes' : 'No';
  if (Array.isArray(cell.value)) return cell.value.length ? cell.value.join(', ') : 'None reported';
  if (field === 'video.title' && cell.value === '') return 'Untitled video';
  if (field === 'video.type') return researchTypeNames[cell.value as ResearchType];
  // Library's 28-day growth reads as Library shows it (card and Inspector), not as raw server numbers.
  if (field === 'observation.growth_28d_status') return GROWTH_STATUS_LABELS[cell.value as keyof typeof GROWTH_STATUS_LABELS] ?? String(cell.value);
  if (field === 'observation.views_gained_28d') return signedCompactCount(String(cell.value)) ?? String(cell.value);
  if (field === 'observation.views_per_day_28d') return averageViewsPerDay(String(cell.value)) ?? String(cell.value);
  if (field === 'observation.growth_28d_interval_seconds') return observedInterval(Number(cell.value), String(cell.value));
  if (field === 'observation.growth_28d_tracked_days') { const days = BigInt(cell.value); return days.toLocaleString('en-US') + (days === 1n ? ' day' : ' days'); }
  if (['video.views', 'video.likes', 'video.comments', 'channel.views', 'channel.subscribers', 'channel.video_count'].includes(field)) return BigInt(cell.value).toLocaleString('en-US');
  if (field === 'video.duration_seconds') { const n = BigInt(cell.value); return String(n / 60n) + ':' + String(n % 60n).padStart(2, '0'); }
  // As the Library Inspector: two places, and a small nonzero rate never reads as zero.
  if (field === 'video.like_rate' || field === 'video.comment_rate') {
    const rate = Number(cell.value);
    return rate > 0 && rate < 0.005 ? '<0.01%' : new Intl.NumberFormat('en', { maximumFractionDigits: 2 }).format(rate) + '%';
  }
  if (field === 'video.title_length') return BigInt(cell.value).toLocaleString('en-US') + ' characters';
  if (field === 'video.tag_count') return BigInt(cell.value).toLocaleString('en-US');
  if (field === 'video.lifetime_views_per_day') return new Intl.NumberFormat('en', { maximumFractionDigits: 2 }).format(Number(cell.value)) + ' views/day';
  if (field.endsWith('_at')) return cell.value.slice(0, 19).replace('T', ' ');
  return cell.value || 'Empty';
}
export function VideoImage({ row, field = 'video.thumbnail_url' }: { row: ResearchRow; field?: ResearchField }) {
  const cell = row.cells[field];
  const url = cell?.state === 'known' && typeof cell.value === 'string' && safeImage(cell.value) ? cell.value : undefined;
  return url ? <img src={url} alt="" loading="lazy" referrerPolicy="no-referrer" onError={event => { event.currentTarget.hidden = true; }} /> : <span className="research-no-image">No image</span>;
}
export function SourceDetails({ row, page }: { row: ResearchRow; page: ResearchPage }) {
  const cell = row.cells['video.type'], type = cell ? page.provenance[cell.provenance_ref] : undefined;
  const basis = row.id.startsWith('UC') ? 'Reported channel values. Subscriber counts may be rounded by YouTube; hidden or unreported counts remain unknown. Profile and country have separate freshness clocks.' : !type ? 'Format evidence was not selected in this query.' : type.method === 'automatic_long_short_v1' && type.type_rule
    ? 'Conflicting Long-form/Shorts playlist membership resolved using the ' + type.type_rule.max_short_seconds + '-second Shorts limit (rule ' + type.type_rule.version + ').'
    : type.method === 'typed_playlist_membership_v1' ? 'Type confirmed by YouTube playlist membership.'
    : type.method === 'manual_decision_v1' ? 'Type follows a recorded manual decision.' : 'Type basis is not reported.';
  return <details className="research-source-details"><summary>Data details</summary><p>{basis}</p>
    <dl>{page.columns.map(field => {
      const value = row.cells[field]!, origin = page.provenance[value.provenance_ref];
      return <div key={field}><dt>{researchLabels[field]}</dt><dd>{value.state} · {origin.origin === 'youtube_reported' ? 'Reported by YouTube' : origin.origin === 'normalized' ? 'Normalized from YouTube data' : origin.origin === 'calculated' ? 'Calculated by Leti Cook from observed YouTube values' : 'Application state'}
        {value.observed_at && <> · Checked {value.observed_at.replace('T', ' ').replace('Z', ' UTC')}</>}
        {value.expires_at && <> · Valid before {value.expires_at.replace('T', ' ').replace('Z', ' UTC')}</>}
        {value.observation?.method === 'video.observed_growth_28d.v1' && <> · 28-day growth: {value.observation.status.replaceAll('_', ' ')}
          {value.observation.from_at && value.observation.to_at ? ' · observed from ' + value.observation.from_at + ' to ' + value.observation.to_at : ''}
          {value.observation.reason ? ' · ' + value.observation.reason.replaceAll('_', ' ') : ''}</>}
        {value.observation && value.observation.method !== 'video.observed_growth_28d.v1' && <>
          {value.observation.reason !== 'known' ? ' · ' + ({missing_endpoint: 'No snapshot matching the current counter', missing_boundary: 'No observation within the allowed boundary', unknown_definition: 'Metric definition was not recorded', incompatible_definition: 'Metric definitions differ', missing_age_observation: 'No observation at age 22–24 hours'}[value.observation.reason] ?? 'Observation unavailable')
            : ' · Observed from ' + value.observation.from_at + ' to ' + value.observation.to_at + ' (' + value.observation.actual_seconds + ' seconds)'}
        </>}
        {origin.definition_epoch && <> · Definition: {origin.definition_epoch}</>}</dd></div>;
    })}</dl>
  </details>;
}
export function CellValue({ row, field }: { row: ResearchRow; field: ResearchField }) {
  const text = displayResearchCell(row.cells[field], field);
  if (field === 'video.thumbnail_url' || field === 'channel.avatar_url') return <VideoImage row={row} field={field} />;
  if (field === 'video.title' || field === 'video.id') return <a href={'https://www.youtube.com/watch?v=' + row.id} target="_blank" rel="noopener noreferrer">{text}</a>;
  if (field === 'channel.title' || field === 'video.channel_id' || field==='channel.id') {
    if(row.id.startsWith('UC')) return <a href={'https://www.youtube.com/channel/'+row.id} target="_blank" rel="noopener noreferrer">{text}</a>;
    const channel = row.cells['video.channel_id'];
    return channel?.state === 'known' ? <a href={'https://www.youtube.com/channel/' + channel.value} target="_blank" rel="noopener noreferrer">{text}</a> : <>{text}</>;
  }
  return <>{text}</>;
}
