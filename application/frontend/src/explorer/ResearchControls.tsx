// Controls and displays shared by the catalogue page and the research workshop, moved from
// ResearchCatalogue.tsx with the same texts: the observed change window, sort keys, columns, coverage in words,
// side-by-side comparison of 2–4 rows, and a calculation block's result (moved from ResearchOverview.tsx).
import type { ReactNode } from 'react';
import { sourceExpired } from '../sourceLifetime';
import { ObservationAgeLimitNote } from './ObservationAgeLimitNote';
import { CellValue, displayResearchCell } from './ResearchRows';
import { observedFields, researchLabels, type FieldDefinition, type ResearchField, type ResearchPage, type ResearchRow } from './researchQuery';
import { summaryOperation,calendarOperation,metrics,type OperationResult,type SummaryResult,type CalendarResult,type HistoryResult } from './researchOperations';

// ---- Observed change window ----
export type ObservationWindow = { version: 1; window_seconds: 3600 | 86400; tolerance_seconds: number; age_seconds: 86400; age_tolerance_seconds: number };
export const observationWindow = (seconds: 3600 | 86400): ObservationWindow => ({ version: 1, window_seconds: seconds,
  tolerance_seconds: seconds === 3600 ? 1800 : 7200, age_seconds: 86400, age_tolerance_seconds: 7200 });
/** The change fields that need a window. */
export const changeFields: readonly string[] = ['observation.views_delta', 'observation.views_per_hour'];

/** limits: the capabilities' observation_age_limits of the study's format: null = every age, 0 = unavailable. */
export function ObservationWindowPicker({ window, limits, onChange }: {
  window: 3600 | 86400; limits?: Record<string, number | null>; onChange: (next: ObservationWindow) => void;
}) {
  const windowLimit = (seconds: number) => limits?.[String(seconds)], limit = windowLimit(window), unit = window === 3600 ? 'hour' : 'day';
  return <p className="research-note"><label>View change over <select value={window} onChange={e => onChange(observationWindow(Number(e.target.value) as 3600 | 86400))}>
    <option value="3600" disabled={windowLimit(3600) === 0}>1 hour</option><option value="86400" disabled={windowLimit(86400) === 0}>1 day</option></select></label>{' '}{limit === 0
      ? `No video is refreshed at least once per ${unit} under the current collection schedule, so this change is not available. Choose another window or remove the change field.`
      : limit !== null && limit !== undefined
      ? `Only videos up to ${Math.round(limit / 86400)} days old are shown: only they are refreshed at least once per ${unit} under the collection schedule.`
      : ''}</p>;
}
/** How observation columns are measured; shown when a column is an observation. */
export function ObservationNote({ window, columns }: { window: 3600 | 86400; columns: readonly (ResearchField | string)[] }) {
  if (!columns.some(field => observedFields.includes(field as ResearchField))) return null;
  return <p className="research-note">Observations use real saved snapshots. View change targets {window === 3600 ? 'an hour' : 'a day'} before the current observation, with up to {window === 3600 ? '30 minutes' : '2 hours'} of extra interval. The hourly value is an average over that actual interval. The age column uses a snapshot at age 22–24 hours, not an estimate at exactly 24 hours. Data details show the actual times and any missing evidence.</p>;
}

// ---- Sort keys, up to three ----
export type SortKey = { field: ResearchField; direction: 'asc' | 'desc'; nulls?: 'last' };
const sortName = (field: string) => (researchLabels as Record<string, string>)[field] ?? field;

/** Missing values sort last and the row ID breaks remaining ties (server rules). A key the server no longer
 *  offers stays visible, marked, until it is changed or removed. */
export function ResearchSortEditor({ value, fields, channels, onChange }: {
  value: SortKey[]; fields: FieldDefinition[]; channels: boolean; onChange: (next: SortKey[]) => void;
}) {
  const sortable = fields.filter(f => f.sortable);
  const set = (index: number, patch: Partial<SortKey>) => onChange(value.map((item, n) => n === index ? { ...item, ...patch } : item));
  return <>{value.map((sort, index) => <div className="research-sort" key={index}>
    <span>{index + 1}.</span><select aria-label={'Sort field ' + (index + 1)} value={sort.field} onChange={e => set(index, { field: e.target.value as ResearchField })}>
      {!sortable.some(f => f.id === sort.field) && <option value={sort.field}>{sortName(sort.field)} (not available)</option>}
      {sortable.map(f => <option key={f.id} value={f.id} disabled={value.some((item, n) => n !== index && item.field === f.id)}>{researchLabels[f.id]}</option>)}
    </select><select aria-label={'Sort direction ' + (index + 1)} value={sort.direction} onChange={e => set(index, { direction: e.target.value as 'asc' | 'desc' })}><option value="desc">Descending</option><option value="asc">Ascending</option></select>
    <button type="button" disabled={value.length === 1} onClick={() => onChange(value.filter((_, n) => n !== index))}>Remove</button>
  </div>)}<button type="button" disabled={value.length >= 3 || sortable.length <= value.length} onClick={() => {
    const field = sortable.find(f => !value.some(item => item.field === f.id));
    if (field) onChange([...value, { field: field.id, direction: 'desc', ...(value[0]?.nulls ? { nulls: 'last' as const } : {}) }]);
  }}>Add sort</button>
    <small>Missing values sort last. {channels ? 'Channel' : 'Video'} ID breaks remaining ties.</small></>;
}

// ---- Columns ----
/** loaded: the server's fields are known, so a missing one is marked "(disabled)". A column this browser
 *  does not know stays listed by its id, marked, so it can be removed. The last column cannot be removed. */
export function ResearchColumnsPicker({ columns, fields, channels, loaded, onChange }: {
  columns: readonly string[]; fields: FieldDefinition[]; channels: boolean; loaded: boolean; onChange: (next: ResearchField[]) => void;
}) {
  const chosen = columns as ResearchField[];
  return <div className="research-columns">{Object.entries(researchLabels).filter(([id]) => channels ? id.startsWith('channel.') : id !== 'channel.id').map(([id, label]) => <label key={id}>
    <input type="checkbox" checked={chosen.includes(id as ResearchField)} disabled={(!fields.some(f => f.id === id) && !chosen.includes(id as ResearchField)) || (chosen.length === 1 && chosen.includes(id as ResearchField))}
      onChange={e => onChange(e.target.checked ? [...chosen, id as ResearchField] : chosen.filter(f => f !== id))} />{label}{loaded && !fields.some(f => f.id === id) ? ' (disabled)' : ''}
  </label>)}{chosen.filter(id => !Object.hasOwn(researchLabels, id)).map(id => <label key={id}>
    <input type="checkbox" checked disabled={chosen.length === 1} onChange={() => onChange(chosen.filter(f => f !== id))} />{id} (not available on this server)
  </label>)}</div>;
}

// ---- Coverage in words ----
export const coverageReasonLabels: Readonly<Record<string, string>> = {
  profiles_incomplete: 'Channels with incomplete or expired profiles',
  inventory_incomplete: 'Channels whose inventory is incomplete',
  source_rows_unavailable: 'Unavailable or excluded video rows',
  required_fields_missing: 'Rows missing required fields',
  collection_members_unavailable: 'Saved videos currently unavailable',
  collection_members_hidden: 'Saved videos excluded because you hid their channel',
  // Fixed lists: rows and values that would not outlive the list.
  source_rows_expiring: 'Rows left out because their source data expires before this fixed list ends',
  rows_with_expired_values: 'Rows with values shown as Expired because they expire before this fixed list ends',
};
export const coverageReasonLabel = (code: string): string => coverageReasonLabels[code] ?? `Other source limitation (${code})`;

/** "Complete source · 5 of 5 channels complete" (videos), "· 3 of 4 profiles current" (channels), or collection members. */
export function coverageSummary(coverage: ResearchPage['coverage'], channels: boolean): string {
  return (coverage.status === 'complete' ? 'Complete source' : 'Partial source') + (coverage.basis !== 'collection_members'
    ? ' · ' + coverage.channels_complete + ' of ' + coverage.channels_in_scope + (channels ? ' profiles current' : ' channels complete')
    : ' · Saved videos checked individually');
}
/** "Why data is incomplete": every reason with a count, in words; nothing when no reason counts. */
export function CoverageReasons({ reasons }: { reasons: Record<string, number> }) {
  const counted = Object.entries(reasons).filter(([, number]) => number > 0);
  if (!counted.length) return null;
  return <details className="research-coverage"><summary>Why data is incomplete</summary><ul>{counted.map(([key, number]) => <li key={key}>{coverageReasonLabel(key)}: {number}</li>)}</ul></details>;
}

// ---- Comparison of 2–4 rows (channels and videos) ----
export const MAX_COMPARED = 4;
export function ResearchCompare({ rows, columns, channels, title, level = 2, children }: {
  rows: ResearchRow[]; columns: readonly ResearchField[]; channels: boolean; title?: string; level?: 2 | 4; children?: ReactNode;
}) {
  const Heading = level === 2 ? 'h2' : 'h4', head = (row: ResearchRow) => channels ? displayResearchCell(row.cells['channel.title'], 'channel.title')
    : row.cells['video.title'] ? displayResearchCell(row.cells['video.title'], 'video.title') : row.id;
  return <section aria-label={channels ? 'Selected channel comparison' : 'Selected video comparison'}><Heading>{title ?? (channels ? 'Compare selected channels' : 'Compare selected videos')}</Heading>
    <p>Same reported values and source times as the current page.{channels ? ' Subscribers may be rounded by YouTube.' : ''} No score or rank is calculated.</p>
    <div className="research-table-wrap"><table><thead><tr><th>Field</th>{rows.map(row => <th key={row.id}>{head(row)} · {row.id}</th>)}</tr></thead>
      <tbody>{columns.map(field => <tr key={field}><th>{researchLabels[field] ?? field}</th>{rows.map(row => <td key={row.id}><CellValue row={row} field={field} /></td>)}</tr>)}</tbody></table></div>
    {children}</section>;
}

// ---- A calculation block's result (fingerprint, publishing calendar, channel history) ----
const metricLabels:Record<string,string>={views:'Views',duration_seconds:'Duration (seconds)',likes:'Likes',comments:'Comments',title_length:'Title length',tag_count:'Tags'};
export function OperationDisplay({value}:{value:OperationResult}){
  if(sourceExpired(value))return <p>These results expired. Run again for current data.</p>;
  return <div className="research-operation-result"><p>{value.coverage.status==='partial'?'Partial source: some data is unavailable.':'Complete source.'} All matching rows, before pagination. Checked {value.context.evaluated_at.replace('T',' ')}.</p>
    <ObservationAgeLimitNote limit={value.context.observation_age_limit_seconds}/>
    {Object.values(value.coverage.reasons).some(n=>n>0)&&<details><summary>Coverage details</summary><ul>{Object.entries(value.coverage.reasons).filter(([,n])=>n>0).map(([key,n])=><li key={key}>{coverageReasonLabel(key)}: {n}</li>)}</ul></details>}
    {value.operation===summaryOperation?(()=>{const r=value.result as SummaryResult;return <><p>{r.population} videos in this selection. Unknown values are excluded separately for each metric. Calculated by Leti Cook from stored YouTube data.</p>
      <div className="research-table-wrap"><table><caption>Channel fingerprint · current matching videos</caption><thead><tr><th>Metric</th><th>Known / missing</th><th>Median</th><th>Average</th><th>Q1–Q3</th><th>Total</th></tr></thead><tbody>{metrics.map(id=>{const m=r.metrics[id];return <tr key={id}><th>{metricLabels[id]}</th><td>{m.known_count} / {m.missing_count}{m.state==='mixed_definition_epochs'&&<small> Incompatible view definitions; aggregate withheld.</small>}{m.state==='unknown_definition_epoch'&&<small> View definition not recorded; aggregate withheld.</small>}{m.definition_epoch&&<small> Definition: {m.definition_epoch}</small>}</td><td>{m.median??'Unknown'}</td><td>{m.mean??'Unknown'}</td><td>{m.q1??'Unknown'} – {m.q3??'Unknown'}</td><td>{m.total??'Unknown'}</td></tr>;})}</tbody></table></div>
      <details><summary>Distributions</summary>{metrics.map(id=><section key={id}><h4>{metricLabels[id]}</h4>{r.metrics[id].buckets.map(b=><div key={b.key} className="research-bar"><span>{b.label}</span><meter min={0} max={Math.max(1,r.metrics[id].known_count)} value={b.count}/><span>{b.count}</span></div>)}</section>)}</details></>;})():value.operation===calendarOperation?(()=>{const r=value.result as CalendarResult;return <><h3>Publishing rhythm · {r.time_zone}</h3><p>{r.known_count} known publication dates in {r.population} matching videos.</p><div className="research-bars">{Array.from({length:24},(_,hour)=>{const n=r.hours.find(p=>p.hour===hour)?.count??0;return <div key={hour} className="research-bar"><span>{String(hour).padStart(2,'0')}:00</span><meter min={0} max={Math.max(1,...r.hours.map(h=>h.count))} value={n}/><span>{n}</span></div>;})}</div><h4>Weekdays</h4><p>{['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map((label,i)=>label+': '+(r.weekdays.find(p=>p.weekday===i+1)?.count??0)).join(' · ')}</p><h3>Publication calendar · {r.year}</h3><div className="research-calendar">{r.days.map(day=><span key={day.date} title={day.date+': '+day.count+' videos'} style={{opacity:.35+.65*day.count/Math.max(1,...r.days.map(d=>d.count))}}>{day.date.slice(5)}<b>{day.count}</b></span>)}</div>{!r.days.length&&<p>No matching publication dates in this year.</p>}</>;})():(()=>{const r=value.result as HistoryResult;return <><h3>Recorded channel {r.metric}</h3><p>{r.observations} real observations; latest point per {r.bucket_hours}-hour UTC bucket. No interpolation or estimated values. Subscriber counts may be rounded. Metric definition was not recorded, so no growth rate is calculated.</p><div className="research-table-wrap"><table><thead><tr><th>Observed (UTC)</th><th>Reported {r.metric}</th></tr></thead><tbody>{r.points.map(p=><tr key={p.recorded_at}><td>{p.recorded_at}</td><td>{BigInt(p.value).toLocaleString('en-US')}</td></tr>)}</tbody></table></div>{!r.points.length&&<p>No retained observations for this selection.</p>}</>;})()}
  </div>;
}
