// The research workshop's block views: the Source block's inspector and a result
// block. The inspector holds every source setting of the catalogue page, written straight into the study's root
// query: row kind, source and its choice, format, conditions, sort, completeness, change window, date zone and how
// rows are read. A result block shows its read time and coverage in words, pages on its own (current: a cursor
// stack; a fixed list: its next/previous cursors), compares 2-4 rows and expires on its own clock.
import { useEffect, useRef, useState } from 'react';
import { ExplorerApiError } from './api';
import { useSourceExpiry } from '../sourceLifetimeReact';
import { ObservationAgeLimitNote } from './ObservationAgeLimitNote';
import { ResearchConditions, conditionCount, firstCondition } from './ResearchConditions';
import { CoverageReasons, MAX_COMPARED, ObservationNote, ObservationWindowPicker, OperationDisplay, ResearchCompare, ResearchSortEditor, changeFields, coverageSummary } from './ResearchControls';
import { CellValue, SourceDetails, VideoImage } from './ResearchRows';
import { blockFilters, studyFields, withScope, withSourceQuery, type BlockPage, type NodeKind, type NodeResult, type ResearchDocument } from './researchGraph';
import type { OperationResult } from './researchOperations';
import { channelSource, researchLabels, researchTypeNames, videoSource, type ResearchCapabilities, type ResearchPage, type ResearchSource, type ResearchType, type ScopeKind } from './researchQuery';
import { blockError, blockTimeLabel, calculationNote, isFrozenPage, loadBlockPage, pagePosition, samePageSession, selectionNote, sideBySide } from './workshopModel';

export type SourceChoice = { id: string; name: string };
export type SourceChoices = { folder: SourceChoice[]; label: SourceChoice[]; channel: SourceChoice[]; collection: SourceChoice[] };
export type CollectionsState = { loading: boolean; error: string; more: boolean; load: (more?: boolean) => void };
export type ReadMode = 'current' | 'frozen';
const types: ResearchType[] = ['long', 'short', 'stream'];
const fieldName = (id: string) => (researchLabels as Record<string, string>)[id] ?? id;
/** Free-text columns wrap inside their table column using the research-text-cell class. */
export const WRAPPED_FIELDS: readonly string[] = ['video.title', 'channel.title', 'video.description', 'video.tags', 'video.topic_urls'];
export const localZone = (): string => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch { return 'UTC'; } };

/** Fields of this study the server does not offer for its format, in words; '' when every field is offered. */
export function studyFieldIssue(document: ResearchDocument, caps: ResearchCapabilities | null): string {
  if (!caps || caps.source_id !== document.query.source.id) return '';
  const channels = document.query.source.id === channelSource, type = document.query.video_types?.[0] ?? 'long', used = studyFields(document);
  const disabledFor = used.filter(id => { const f = caps.fields.find(x => x.id === id); return f && !channels && f.enabled_video_types && !f.enabled_video_types.includes(type); });
  const off = used.filter(id => !caps.fields.some(f => f.id === id) && Object.hasOwn(researchLabels, id));
  const unknown = used.filter(id => !caps.fields.some(f => f.id === id) && !Object.hasOwn(researchLabels, id));
  return [
    disabledFor.length ? `Disabled for ${researchTypeNames[type]}: ${disabledFor.map(fieldName).join(', ')}. Remove them or enable them in Collection fields.` : '',
    off.length ? `Disabled: ${off.map(fieldName).join(', ')}. Remove them or enable them in Collection fields.` : '',
    unknown.length ? `Not available on this server: ${unknown.join(', ')}. Remove them to run this study.` : '',
  ].filter(Boolean).join(' ');
}
/** The study's fields the server offers for its row kind and format (conditions and sort choose from these). */
export function offeredFields(document: ResearchDocument, caps: ResearchCapabilities | null) {
  if (!caps || caps.source_id !== document.query.source.id) return [];
  const channels = document.query.source.id === channelSource, type = document.query.video_types?.[0] ?? 'long';
  return caps.fields.filter(f => channels || !f.enabled_video_types || f.enabled_video_types.includes(type));
}

export function ResearchSourcePicker({ document, caps, sources, collections, notice, mode, modeNote, onChange, onRows, onMode }: {
  document: ResearchDocument; caps: ResearchCapabilities | null; sources?: SourceChoices; collections?: CollectionsState; notice?: string;
  mode: ReadMode; modeNote: string; onChange: (next: ResearchDocument) => void; onRows: (source: ResearchSource) => void; onMode: (mode: ReadMode) => void;
}) {
  const query = document.query, channels = query.source.id === channelSource, scope = query.scope, type = query.video_types?.[0] ?? 'long';
  const [find, setFind] = useState('');
  const fields = offeredFields(document, caps), where = query.where ?? null, conditions = conditionCount(where);
  const patch = (value: Parameters<typeof withSourceQuery>[1]) => onChange(withSourceQuery(document, value));
  const kinds: ScopeKind[] = channels ? ['channel', 'folder', 'label'] : ['channel', 'folder', 'label', 'collection'];
  const choices = sources ? sources[scope.kind] : [], needle = find.trim().toLocaleLowerCase();
  // The list filter never hides the chosen source.
  const shown = needle ? choices.filter(c => c.id === scope.id || c.name.toLocaleLowerCase().includes(needle)) : choices;
  const zone = query.time?.timezone ?? 'UTC', local = localZone(), window = query.observations?.window_seconds ?? 3600;
  const changeShown = studyFields(document).some(field => changeFields.includes(field)), issue = studyFieldIssue(document, caps);
  return <>
    <p>Uses the study's complete query. Filters and calculations happen before the displayed page is selected.</p>
    {notice && <p role="status">{notice}</p>}
    <div className="research-actions" role="group" aria-label="Rows of this study"><span>Rows</span>
      {([videoSource, channelSource] as ResearchSource[]).map(id => <button type="button" key={id} aria-pressed={query.source.id === id}
        onClick={() => { if (query.source.id !== id) onRows(id); }}>{id === channelSource ? 'Channels' : 'Videos'}</button>)}</div>
    {sources && <>
      <label>Source<select value={scope.kind} onChange={e => { const kind = e.target.value as ScopeKind; setFind(''); if (kind === 'collection') collections?.load(); onChange(withScope(document, { kind, id: '' })); }}>
        {kinds.map(kind => <option key={kind} value={kind} disabled={caps !== null && !caps.scopes.includes(kind)}>{kind[0].toUpperCase() + kind.slice(1)}</option>)}</select></label>
      {choices.length > 12 && <label>Find {scope.kind}<input type="search" value={find} maxLength={120} onChange={e => setFind(e.target.value)} placeholder="Part of the name" /></label>}
      <label>Choose {scope.kind}<select value={scope.id} onChange={e => onChange(withScope(document, { kind: scope.kind, id: e.target.value }))}>
        <option value="">Choose…</option>{scope.id && !choices.some(c => c.id === scope.id) && <option value={scope.id}>{scope.id}</option>}
        {shown.map(choice => <option key={choice.id} value={choice.id}>{choice.name}</option>)}</select></label>
      {scope.kind === 'collection' && collections && <div className="research-actions">
        {collections.error && <span role="status">{collections.error}</span>}
        <button type="button" disabled={collections.loading} onClick={() => collections.load()}>{collections.loading ? 'Loading collections…' : 'Refresh collections'}</button>
        {collections.more && <button type="button" disabled={collections.loading} onClick={() => collections.load(true)}>More collections</button>}
      </div>}
    </>}
    {!channels && <label>Format<select value={type} onChange={e => patch({ video_types: [e.target.value as ResearchType] })}>
      {types.map(t => <option key={t} value={t} disabled={caps !== null && caps.source_id === query.source.id && !caps.video_types.includes(t)}>{researchTypeNames[t]}</option>)}</select></label>}
    {!channels && type === 'stream' && <p className="research-note">Completed streams only. Scheduled and live broadcasts are not part of this catalogue.</p>}
    {issue && <p role="status">{issue}</p>}
    <details open={conditions > 0}><summary>Conditions {conditions ? '(' + conditions + ')' : '(none)'}</summary>
      {where && fields.length ? <ResearchConditions value={where} fields={fields} budget={64 - conditions} onChange={next => patch({ where: next })} />
        : where ? <p role="status">Loading the fields this server offers…</p>
        : <button type="button" disabled={!fields.length} onClick={() => patch({ where: { all: [firstCondition(fields)] } })}>Add condition</button>}
      <small>Dates use UTC. Unknown values are distinct from zero, false and empty values. Use state conditions to include missing data.</small>
    </details>
    <details><summary>Sort ({query.order_by.length})</summary><ResearchSortEditor value={query.order_by} fields={fields} channels={channels}
      onChange={order => patch({ order_by: order.map(item => ({ ...item, nulls: 'last' as const })) })} /></details>
    <label className="research-check"><input type="checkbox" checked={query.coverage?.mode === 'require_complete'}
      onChange={e => patch({ coverage: { ...(query.coverage ?? {}), mode: e.target.checked ? 'require_complete' : 'available' } })} />Require complete data</label>
    {!channels && changeShown && <ObservationWindowPicker window={window} limits={caps?.source_id === query.source.id ? caps.observation_age_limits[type] : undefined}
      onChange={observations => patch({ observations })} />}
    {!channels && <ObservationNote window={window} columns={query.select} />}
    <label>Dates and years<select value={zone} onChange={e => patch({ time: { mode: 'current', timezone: e.target.value } })}>
      <option value="UTC">UTC</option>{local !== 'UTC' && <option value={local}>Local ({local})</option>}{zone !== 'UTC' && zone !== local && <option value={zone}>{zone}</option>}
    </select></label><small>Calendar dates and years use this zone. Times in cells and conditions stay in UTC.</small>
    <div className="research-actions" role="group" aria-label="How rows are read"><span>Rows of this study</span>
      <button type="button" aria-pressed={mode === 'current'} onClick={() => onMode('current')}>Current reads</button>
      <button type="button" aria-pressed={mode === 'frozen'} onClick={() => onMode('frozen')}>Fixed list (10 min)</button></div>
    <small>{modeNote}</small>
  </>;
}

const clockOf = (iso: string) => iso.slice(11, 19);
const listExpired = (validBefore: string) => `The fixed list expired at ${clockOf(validBefore)} UTC. Your study is kept; Run research to fix a new list (counts may differ).`;
/** One result block of the last run. It is remounted for every run, so its pages and selection start fresh. */
export function ResearchBlockResult({ id, type, document, result, fixedStudy, onKeepComparison, onUseCurrentReads }: {
  id: string; type: NodeKind; document: ResearchDocument; result: NodeResult; fixedStudy: boolean;
  onKeepComparison?: (ids: string[]) => void; onUseCurrentReads?: () => void;
}) {
  const channels = document.query.source.id === channelSource;
  const first = result.state === 'ready' && result.output === 'page' ? result.value as BlockPage : null;
  const [view, setView] = useState(() => first ? { page: first, cursors: [undefined] as (string | undefined)[], starts: [0] } : null);
  const [expired, setExpired] = useState(''), [loading, setLoading] = useState(false), [message, setMessage] = useState('');
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);
  const shown: BlockPage | OperationResult | undefined = result.state !== 'ready' ? undefined : first ? view?.page : result.value as OperationResult;
  // Each block expires on its own clock: a fixed list lives up to 10 minutes, current reads up to 2.
  useSourceExpiry(expired ? null : shown, () => {
    pending.current?.abort(); setLoading(false);
    const context = (shown as { context: { valid_before: string; consistency?: string } }).context;
    setExpired(context.consistency === 'frozen' ? listExpired(context.valid_before)
      : `This block's data expired at ${clockOf(context.valid_before)} UTC. Your study is kept; Run research to read current data.`);
  });
  if (result.state === 'error') return <div className="research-actions"><p role="status">{blockError(result.code, result.path, result.limit)}</p>
    {result.code === 'query_budget_exceeded' && result.path === 'consistency' && onUseCurrentReads && <button type="button" onClick={onUseCurrentReads}>Use current reads</button>}</div>;
  if (expired) return <p role="status">{expired}</p>;
  if (!first || !view) {
    const value = result.value as OperationResult;
    return <><p className="research-note">{blockTimeLabel(value.context)}</p>{fixedStudy && <p className="research-note">{calculationNote(value.context.evaluated_at)}</p>}<OperationDisplay value={value} /></>;
  }
  const page = view.page, frozen = isFrozenPage(page), offset = view.starts[view.starts.length - 1], query = result.query;
  async function go(after: string | undefined, cursors: (string | undefined)[], starts: number[]) {
    if (!query) return;
    pending.current?.abort(); const controller = new AbortController(); pending.current = controller;
    setLoading(true); setMessage('');
    let timedOut = false; const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 15000);
    try {
      const next = await loadBlockPage(query, blockFilters(document, id), after, controller.signal, () => setMessage('Stored data is being prepared in the background…'));
      if (pending.current !== controller) return;
      if (!samePageSession(page, next)) throw new Error('This page belongs to another read. Run research again.');
      setView({ page: next, cursors, starts }); setCompareIds([]); setMessage('');
    } catch (error) {
      if (pending.current !== controller) return;
      if (error instanceof ExplorerApiError && error.code === 'cursor_expired' && frozen) setExpired(listExpired(page.context.valid_before));
      else setMessage(timedOut ? 'The page did not arrive in time. Try again.' : error instanceof ExplorerApiError ? blockError(error.code)
        : error instanceof Error && error.message ? error.message : 'The page could not be loaded.');
    } finally { clearTimeout(timer); if (pending.current === controller) setLoading(false); }
  }
  // A fixed list pages with its own cursors; current pages keep a stack of the cursors they were read with.
  const next = () => frozen ? void go(page.page.next_cursor ?? undefined, view.cursors, [...view.starts, page.page.start_index + page.rows.length])
    : void go(page.page.next_cursor ?? undefined, [...view.cursors, page.page.next_cursor ?? undefined], [...view.starts, offset + page.rows.length]);
  const previous = () => frozen ? void go(page.page.previous_cursor ?? undefined, view.cursors, view.starts.slice(0, -1))
    : void go(view.cursors[view.cursors.length - 2], view.cursors.slice(0, -1), view.starts.slice(0, -1));
  const hasPrevious = frozen ? page.page.previous_cursor !== null : view.cursors.length > 1;
  const rows = page.rows, columns = page.columns, details = page as ResearchPage, selected = rows.filter(row => compareIds.includes(row.id));
  const caption = (channels ? 'Channels' : researchTypeNames[(document.query.video_types?.[0] ?? 'long') as ResearchType]) + ' · stored YouTube values and explicitly labelled observations';
  const note = selectionNote(document, id, page, frozen ? page.page.start_index : offset);
  const together = type === 'table' && sideBySide(document, id) && rows.length > 0 && rows.length <= MAX_COMPARED;
  const label = (field: string) => (researchLabels as Record<string, string>)[field] ?? field;
  return <div className="workshop-block-result">
    <p className="research-note">{blockTimeLabel(page.context)} · {coverageSummary(page.coverage, channels)} · {pagePosition(page, view.cursors.length, channels)}</p>
    {note && <p className="research-note">{note}</p>}
    <CoverageReasons reasons={page.coverage.reasons} /><ObservationAgeLimitNote limit={page.context.observation_age_limit_seconds} />
    {together ? <><ResearchCompare rows={rows} columns={columns} channels={channels} title="Side by side" level={4} />
      <ul className="workshop-details">{rows.map(row => <li key={row.id} data-video-id={row.id}><CellValue row={row} field={channels ? 'channel.id' : 'video.id'} /> <SourceDetails row={row} page={details} /></li>)}</ul></>
    : type === 'gallery' ? <div className="research-gallery">{rows.map(row => <article key={row.id} data-video-id={row.id}>
      <a className="research-thumbnail" href={(channels ? 'https://www.youtube.com/channel/' : 'https://www.youtube.com/watch?v=') + row.id} target="_blank" rel="noopener noreferrer"><VideoImage row={row} field={channels ? 'channel.avatar_url' : 'video.thumbnail_url'} /></a>
      <h4><CellValue row={row} field={channels ? 'channel.title' : 'video.title'} /></h4>
      <dl>{columns.filter(f => !f.endsWith('_url') && f !== (channels ? 'channel.title' : 'video.title')).map(field => <div key={field}><dt>{label(field)}</dt><dd><CellValue row={row} field={field} /></dd></div>)}</dl>
      <SourceDetails row={row} page={details} /></article>)}</div>
    : <div className="research-table-wrap"><table><caption>{caption}</caption><thead><tr>{columns.map(field => <th key={field} scope="col">{label(field)}</th>)}<th scope="col">Details</th><th scope="col">Compare</th></tr></thead>
      <tbody>{rows.map(row => <tr key={row.id} data-video-id={row.id}>{columns.map(field => <td key={field} className={WRAPPED_FIELDS.includes(field) ? 'research-text-cell' : undefined}><CellValue row={row} field={field} /></td>)}<td><SourceDetails row={row} page={details} /></td>
        <td><input type="checkbox" aria-label={'Compare ' + row.id} checked={compareIds.includes(row.id)} disabled={!compareIds.includes(row.id) && compareIds.length >= MAX_COMPARED}
          onChange={e => setCompareIds(ids => e.target.checked ? [...ids, row.id] : ids.filter(item => item !== row.id))} /></td></tr>)}</tbody></table></div>}
    {selected.length > 0 && <ResearchCompare rows={selected} columns={columns} channels={channels} level={4}>
      {onKeepComparison && <div className="research-actions"><button type="button" disabled={selected.length < 2} onClick={() => onKeepComparison(selected.map(row => row.id))}>Keep as comparison</button>
        <small>Adds a Selected IDs block with these {channels ? 'channels' : 'videos'} and a table that shows them side by side.</small></div>}
    </ResearchCompare>}
    {!rows.length && <p>No {frozen ? '' : 'current '}rows match this block.</p>}
    {message && <p role="status">{message}</p>}
    <footer className="research-actions"><small>{frozen ? 'Pages come from this fixed list; they do not change while it lasts.' : 'Live pages: updates may move rows between pages.'}</small>
      <button type="button" disabled={!hasPrevious || loading || !query} onClick={previous}>← Previous</button>
      <button type="button" disabled={!page.page.has_more || loading || !query} onClick={next}>Next →</button></footer>
  </div>;
}
