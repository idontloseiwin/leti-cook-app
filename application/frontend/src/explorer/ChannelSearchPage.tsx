import { useEffect, useRef, useState, type FormEvent } from 'react';
import { requestJson } from './api';
import { YouTubeAttribution } from '../components/YouTubeAttribution';
import type { ExplorerAddChannelResult } from './model';
import './channelSearch.css';

type Mode = 'topic' | 'channel' | 'category';
type Cost = Partial<Record<'search_queries' | 'general' | 'video_batch_get_stats', number>>;
type Hit = {position?: number | null; kind: 'video' | 'channel'; id: string; channel: string | null; title: string | null;
  author: string | null; thumbnail: string | null; description: string | null; published: string | null};
type Count = number | string | null;
type Profile = {name: string | null; subscribers: Count; videos: Count; created: string | null};
type Library = 'new' | 'removed' | 'tracked' | 'unknown';
type Step = {cost: Cost; stop: string | null; rejected: number};
type Page = {id: string; valid_for_ms: number; expiresAt: number; search_step: Step; hits: Hit[]; profiles: Record<string, Profile>; views: Record<string, Count>;
  library: Record<string, Library>; steps: Record<string, Step>;
  retry_cost: Cost; more: boolean; retry: boolean};
type Props = {onAdd: (id: string, signal?: AbortSignal) => Promise<ExplorerAddChannelResult>; onOpen: (id: string) => void};

const search = 'search_queries', general = 'general', stats = 'video_batch_get_stats';
const labels = {search_queries: 'Search', general: 'General', video_batch_get_stats: 'Stats'} as const;
const buckets = [search, general, stats] as const;
const hasNextPage = (page: Page) => page.more && (!page.search_step.stop ||
  ['budget_requests', 'budget_units', 'budget_rows'].includes(page.search_step.stop));
const formatCost = (value: Cost) => buckets.filter(k => value[k]).map(k => value[k] + ' ' + labels[k]).join(' · ') || '0';
const sameCost = (a: Cost, b: Cost) => buckets.every(k => (a[k] ?? 0) === (b[k] ?? 0)) && Object.keys(a).every(k => k in labels);
const number = (n?: Count) => n == null ? 'Unknown' : BigInt(n).toLocaleString();
type SearchTicket = {action_id: string; max_cost: Cost};
const pendingKey = 'leti-cook.pending-channel-search';
let tabTicket: SearchTicket | null = null;

function rememberTicket(ticket: SearchTicket | null) {
  tabTicket = ticket;
  try {
    if (ticket) window.sessionStorage.setItem(pendingKey, JSON.stringify(ticket));
    else window.sessionStorage.removeItem(pendingKey);
  } catch { /* The open tab can still recover the action from memory. */ }
}

function pendingTicket(): SearchTicket | null {
  try {
    const stored = window.sessionStorage.getItem(pendingKey);
    if (!stored) return tabTicket;
    const value = JSON.parse(stored);
    if (typeof value?.action_id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value.action_id) &&
        value.max_cost && typeof value.max_cost === 'object' && !Array.isArray(value.max_cost) &&
        Object.keys(value.max_cost).every(k => buckets.includes(k as keyof Cost)) &&
        value.max_cost.search_queries === 1 && value.max_cost.general === 1 &&
        (value.max_cost.video_batch_get_stats === undefined || value.max_cost.video_batch_get_stats === 1)) return value;
  } catch { /* Storage can be disabled for this site. */ }
  return tabTicket;
}
function thumbnail(url: string | null) {
  if (!url) return;
  try {
    const u = new URL(url);
    if (u.protocol === 'https:' && ['i.ytimg.com','img.youtube.com','yt3.ggpht.com','yt3.googleusercontent.com'].includes(u.hostname)) return u.href;
  } catch { /* The other source fields are still usable. */ }
}
async function post(action: string, data: unknown, signal: AbortSignal) {
  return requestJson('/sources/channel-search/' + action, signal, {method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Saved-Work': '1'}, body: JSON.stringify(data)});
}

export default function ChannelSearchPage({onAdd, onOpen}: Props) {
  const [mode, setMode] = useState<Mode>('topic'), [q, setQ] = useState(''), [category, setCategory] = useState('1');
  const [after, setAfter] = useState(''), [withViews, setWithViews] = useState(true);
  const [minViews, setMinViews] = useState(''), [maxSubs, setMaxSubs] = useState('');
  const [pages, setPages] = useState<Page[]>([]), [spent, setSpent] = useState<Cost>({});
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [pageCost, setPageCost] = useState<Cost>({});
  const [pending, setPending] = useState<SearchTicket | null>(pendingTicket);
  function keepPending(value: SearchTicket | null) {
    rememberTicket(value);
    setPending(value);
  }
  const [allowance, setAllowance] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    void requestJson('/sources/acquisitions/status', controller.signal).then(value => {
      if (controller.signal.aborted) return;
      const caps = (value as {daily_caps?: Record<string, {remaining_units?: number | null}>})?.daily_caps;
      setAllowance('Local daily allowance remaining: Search ' + number(caps?.[search]?.remaining_units) +
        ' · General ' + number(caps?.[general]?.remaining_units) + '. Other clients can spend the project quota.');
    }).catch(() => {if (!controller.signal.aborted) setAllowance('Local daily allowance unavailable.');});
    return () => controller.abort();
  }, [spent]);
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  const expiresAt = pages[0]?.expiresAt;
  useEffect(() => {
    if (expiresAt == null) return;
    const expire = () => {
      if (Date.now() < expiresAt) return;
      setPages([]); setSpent({}); setNotice('Search results expired. Run a new search to refresh them.');
    };
    const timer = window.setTimeout(expire, Math.max(0, expiresAt - Date.now()));
    window.addEventListener('focus', expire);
    return () => {window.clearTimeout(timer); window.removeEventListener('focus', expire);};
  }, [expiresAt]);
  const price: Cost = {[search]: 1, [general]: 1, ...(mode !== 'channel' && withViews ? {[stats]: 1} : {})};

  async function submit(action: 'first' | 'more' | 'retry', parent?: Page, recover?: SearchTicket) {
    if (active.current || (pendingTicket() && !recover)) return;
    const controller = new AbortController(); active.current = controller; setBusy(true); setError(''); setNotice('');
    const expected = action === 'first' ? price : action === 'more' ? pageCost : parent!.retry_cost;
    const data = action === 'first' ? {mode, ...(mode === 'category' ? {category} : {q: q.trim()}),
      stats: mode !== 'channel' && withViews, ...(mode !== 'channel' && after ? {after: after + 'T00:00:00Z'} : {})}
      : {parent: parent!.id, action};
    try {
      const ticket = recover ?? await post('preview', data, controller.signal) as SearchTicket;
      if (controller.signal.aborted) return;
      if (!ticket.action_id || !ticket.max_cost || (!recover && !sameCost(ticket.max_cost, expected))) throw new Error('The cost estimate changed. Refresh before searching.');
      if (action === 'first') keepPending(ticket);
      const sentAt = Date.now();
      const result = await post('execute', {action_id: ticket.action_id, confirmed_cost: ticket.max_cost}, controller.signal) as Page;
      if (controller.signal.aborted) return;
      if (!Array.isArray(result?.hits) || !result.steps || !result.profiles || !result.views || !result.library || !result.search_step ||
          !Object.values(result.steps).every(step => step && step.cost && Object.entries(step.cost).every(([key, value]) =>
            buckets.includes(key as keyof Cost) && Number.isSafeInteger(value) && value >= 0)) ||
          !Number.isFinite(result.valid_for_ms)) throw new Error('The search response was incomplete.');
      const page = {...result, id: ticket.action_id, expiresAt: sentAt + result.valid_for_ms};
      if (Date.now() >= page.expiresAt) throw new Error('Search expired. Run a new search.');
      setPages(old => action === 'first' ? [page] : action === 'more' ? [...old, page] : old.map(p => p.id === parent!.id ? page : p));
      if (action === 'first') {keepPending(null); setPageCost(ticket.max_cost); setMaxSubs(''); setMinViews('');}
      setSpent(old => {
        const sum: Cost = action === 'first' ? {} : {...old};
        for (const step of Object.values(result.steps)) for (const b of buckets) sum[b] = (sum[b] ?? 0) + (step.cost[b] ?? 0);
        return sum;
      });
    } catch (e) {
      if (!controller.signal.aborted) {
        const code = (e as {code?: string} | null)?.code;
        if (code === 'search_action_expired' || code === 'search_action_uncertain') {
          setError('The server cannot recover this search. Discard the request to start a new one with a separate quota allowance.');
        } else if (code === 'search_action_running') {
          setError('The original search is still running. Resume it again in a moment.');
        } else setError(e instanceof Error ? e.message : 'Search failed.');
      }
    } finally {
      if (active.current === controller) {active.current = null; setBusy(false);}
    }
  }

  async function add(id: string) {
    if (active.current) return;
    const controller = new AbortController(); active.current = controller; setBusy(true); setError('');
    try {
      const reply = await onAdd(id, controller.signal);
      if (reply.channel?.channel_id !== id || !['added','restored','exists'].includes(reply.status ?? '')) {
        throw new Error(reply.error || 'Library did not confirm this channel.');
      }
      setPages(old => old.map(p => ({...p, library: {...p.library, [id]: 'tracked'}})));
      setNotice(reply.status === 'restored' ? 'Channel restored to Library.' : 'Channel is in Library.');
    } catch (e) {if (!controller.signal.aborted) setError(e instanceof Error ? e.message : 'Library action failed.');}
    finally {active.current = null; setBusy(false);}
  }

  const authors = new Map<string, Hit[]>(), profiles: Record<string, Profile> = {}, views: Record<string, Count> = {};
  const membership: Record<string, Library> = {}, seen = new Set<string>(), hits: Hit[] = [];
  for (const p of pages) {
    Object.assign(profiles, p.profiles); Object.assign(views, p.views); Object.assign(membership,p.library);
    for (const h of p.hits) {
      if (seen.has(h.kind + h.id)) continue;
      seen.add(h.kind + h.id); hits.push(h);
      if (h.channel) {const examples = authors.get(h.channel) ?? []; examples.push(h); authors.set(h.channel, examples);}
    }
  }
  const last = pages.at(-1), hasVideoStats = !!pageCost[stats];
  const enter = (e: FormEvent) => {e.preventDefault(); void submit('first');};
  const matches = Array.from(authors).filter(([id, example]) => {
    const subs = profiles[id]?.subscribers;
    return (!maxSubs || (subs != null && BigInt(subs) <= BigInt(maxSubs))) &&
      (!minViews || example.some(h => views[h.id] != null && BigInt(views[h.id]!) >= BigInt(minViews)));
  });
  return <section className="channel-finder" aria-label="Channel search">
    <h1>Find channels</h1>
    <form onSubmit={enter} className="finder-form">
      <label>Find by<select value={mode} onChange={e => setMode(e.target.value as Mode)}>
        <option value="topic">Videos about a topic</option><option value="channel">Channel name or keywords</option>
        <option value="category">Video category</option></select></label>
      {mode === 'category' ? <label>YouTube category ID<input required inputMode="numeric" list="finder-categories" pattern="[0-9]{1,4}" value={category} onChange={e => setCategory(e.target.value)} />
        <datalist id="finder-categories"><option value="1">Film &amp; Animation</option><option value="10">Music</option>
          <option value="17">Sports</option><option value="20">Gaming</option><option value="27">Education</option>
          <option value="28">Science &amp; Technology</option></datalist></label>
        : <label>{mode === 'topic' ? 'Video topic' : 'Channel keywords'}<input required maxLength={200} value={q} onChange={e => setQ(e.target.value)} /></label>}
      {mode !== 'channel' && <><label>Published since (UTC)<input type="date" value={after} onChange={e => setAfter(e.target.value)} /></label>
        <label className="finder-check"><input type="checkbox" checked={withViews} onChange={e => setWithViews(e.target.checked)} /> Include video views</label></>}
      <button disabled={busy || !!pending || (mode !== 'category' && !q.trim())}>Search YouTube</button>
      <small>Up to {formatCost(price)} per page</small>
    </form>
    <small>{allowance}</small>
    {pending && !busy && <div>
      <p>The first search has no confirmed result. Resume it with its original quota allowance. Starting over can spend quota again.</p>
      <button onClick={() => void submit('first', undefined, pending)}>Resume original search</button>
      <button onClick={() => {keepPending(null); setError('');}}>Discard request</button>
    </div>}
    {busy && <p role="status">Working…</p>}{error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {last && <><p>Loaded {pages.length} page(s) · {hits.length} distinct results · Used {formatCost(spent)}</p>
      {pages.map(p => <div key={p.id}>{Object.entries({...p.steps, [search]: p.search_step}).map(([k, s]) => <div key={k}>
        {s.stop && !(k === search && hasNextPage(p)) &&
          <p role="status">{labels[k as keyof Cost]}: {s.stop.replaceAll('_',' ')}</p>}
        {s.rejected > 0 && <p>{s.rejected} result(s) could not be read.</p>}</div>)}
        {p.retry && <button disabled={busy || !!pending} onClick={() => void submit('retry',p)}>Retry missing details · up to {formatCost(p.retry_cost)}</button>}</div>)}
      <YouTubeAttribution label="Open YouTube" />
      <details><summary>YouTube search results ({pages.reduce((count, p) => count + p.hits.length, 0)})</summary>{pages.map((p, pageIndex) => <ol key={p.id} aria-label={'YouTube page ' + (pageIndex + 1)}>{p.hits.map((h,i) =>
        <li key={p.id + i} value={h.position ?? undefined}><a target="_blank" rel="noreferrer" href={'https://www.youtube.com/' + (h.kind === 'channel' ? 'channel/' : 'watch?v=') + encodeURIComponent(h.id)}>{h.title || h.id}</a>
        {thumbnail(h.thumbnail) && <img className="finder-preview" src={thumbnail(h.thumbnail)} alt="" loading="lazy" referrerPolicy="no-referrer"/>}
        {h.author && <span> · {h.author}</span>}{h.description && <p>{h.description}</p>}</li>)}</ol>)}</details>
      <h2>Channels in these results</h2>
      <div className="finder-form"><label>Maximum subscribers<input inputMode="numeric" value={maxSubs} onChange={e => setMaxSubs(e.target.value.replace(/\D/g,'').slice(0,20))}/></label>
        {hasVideoStats && <label>Minimum views on a matching video<input inputMode="numeric" value={minViews} onChange={e => setMinViews(e.target.value.replace(/\D/g,'').slice(0,20))}/></label>}</div>
      {(maxSubs || minViews) && <p>Filters use known values on the loaded pages. Showing {matches.length} of {authors.size} channels.</p>}
      {!last.hits.length && !last.search_step.rejected && hasNextPage(last) &&
        <p>This page has no results. Another page is available.</p>}
      {!matches.length && (hits.length > 0 || last.search_step.rejected || !hasNextPage(last)) ? <p>{hits.length
        ? 'No matching channels on these pages.'
        : pages.some(p => p.search_step.rejected || (p.search_step.stop && !hasNextPage(p)))
          ? 'The search did not return a usable page. See its status above.' : 'YouTube returned no results.'}</p> : null}
      <ul className="finder-cards">{matches.map(([id, examples]) => {
        const img=examples[0].kind === 'channel' ? thumbnail(examples[0].thumbnail) : undefined, profile=profiles[id], state=membership[id];
        return <li key={id}><div>{img && <img src={img} alt="" loading="lazy" referrerPolicy="no-referrer"/>}
          <h3><a href={'https://www.youtube.com/channel/' + encodeURIComponent(id)} target="_blank" rel="noreferrer">{profile?.name || examples[0].author || id}</a></h3>
          <p>{number(profile?.subscribers)} subscribers {profile?.created && ' · Created ' + profile.created.slice(0,10)}</p>
          {state === 'tracked' ? <button onClick={() => onOpen(id)}>Open in Library</button>
            : <button disabled={busy || !state || state === 'unknown'} onClick={() => void add(id)}>{state === 'removed' ? 'Restore to Library' : state === 'new' ? 'Add to Library' : 'Library status unavailable'}</button>}</div>
          {examples.filter(h => h.kind === 'video').map(h => <p key={h.id}><a href={'https://www.youtube.com/watch?v=' + encodeURIComponent(h.id)} target="_blank" rel="noreferrer">{h.title || h.id}</a>
          {hasVideoStats && ' · '+number(views[h.id])+' views'}</p>)}</li>;
      })}</ul>
      {last.more && <button disabled={busy || !!pending} onClick={() => void submit('more',last)}>{hasNextPage(last) ? 'Load next page' : 'Retry this search page'} · up to {formatCost(pageCost)}</button>}
    </>}
  </section>;
}
