import './alerts.css';
import { useEffect, useState } from 'react';
import { requestJson } from './api';
import { sourceExpired, transferSourceLifetime } from '../sourceLifetime';
import { useSourceExpiry } from '../sourceLifetimeReact';
import type { TranslatedQuery } from './libraryData/wire';

export const BASE = '/research/alerts';
export const eventNames = {
  'video.views': 'Video views', 'video.likes': 'Video likes', 'video.comments': 'Video comments',
  'channel.subscribers': 'Channel subscribers', 'video.published': 'New publication',
  'video.unavailable': 'Video became unavailable', 'video.restored': 'Video became available again',
};
type EventName = keyof typeof eventNames;
type ScopeKind = 'channel' | 'folder' | 'label' | 'video';
export type Rule = { name: string; event: EventName; scope: { kind: ScopeKind; id: string }; direction?: 'up' | 'down';
  threshold?: string; video_type?: 'long' | 'short'; min_duration?: number; max_duration?: number };
export type Stored = { id: string; version: number; enabled: boolean; source_available: boolean; definition: Rule; gap_count: number; last_gap_at?: string };
type Selection = Pick<Rule,'scope'|'video_type'|'min_duration'|'max_duration'>;
const counters = new Set<EventName>(['video.views','video.likes','video.comments','channel.subscribers']);
export const fmt = (when: string) => new Date(when).toLocaleString();
export const errorText = (error: unknown) => error instanceof Error ? error.message : 'The request failed.';
export async function write<T>(path: string, value: unknown, method='POST'): Promise<T> {
  return await requestJson(BASE + path, undefined, { method, headers: {'Content-Type':'application/json'},
    ...(value === undefined ? {} : {body: JSON.stringify(value)}) }) as T;
}

function selection(query: TranslatedQuery): Selection {
  const type = query.video_types[0];
  if (type !== 'long' && type !== 'short') throw Error('Alerts support Long-form and Shorts.');
  if (!['channel','folder','label'].includes(query.scope.kind) || !query.scope.id) {
    throw Error('Select a channel, folder or label to watch.');
  }
  const result: Selection = { scope: query.scope, video_type: type };
  function condition(raw: unknown) {
    if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) throw Error('Unsupported filter.');
    const item = raw as Record<string,unknown>;
    if (Array.isArray(item.all) && Object.keys(item).length === 1) { item.all.forEach(condition); return; }
    if (item.field === 'video.duration_seconds' && (item.op === 'gte' || item.op === 'lte')
        && typeof item.value === 'string' && /^\d+$/.test(item.value) && Number.isSafeInteger(Number(item.value))) {
      if (item.op === 'gte') result.min_duration = Math.max(result.min_duration ?? 0, Number(item.value));
      else result.max_duration = Math.min(result.max_duration ?? Number.MAX_SAFE_INTEGER, Number(item.value));
      return;
    }
    throw Error(`This filter cannot be monitored yet: ${String(item.field ?? 'filter group')}. Change it in Library to continue.`);
  }
  if (query.where != null) condition(query.where);
  return result;
}

export function Editor({initial, stored, onClose, onSaved, locked=false}: {
  initial: Rule; stored?: Stored; onClose: ()=>void; onSaved: ()=>void; locked?: boolean;
}) {
  const [rule,set] = useState(initial);
  const [id] = useState(() => crypto.randomUUID());
  const [error,setError] = useState('');
  const [busy,setBusy] = useState(false);
  const [preview,setPreview] = useState<{labels:string[];until:number;at:string}>();
  const [formats,setFormats] = useState<NonNullable<Rule['video_type']>[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    void requestJson(BASE+'/capabilities',controller.signal).then(v=>{
      if (!controller.signal.aborted) setFormats((v as {formats: NonNullable<Rule['video_type']>[]}).formats);
    })
      .catch(e=>{if(!controller.signal.aborted) setError(errorText(e));});
    return ()=>controller.abort();
  },[]);
  useSourceExpiry(preview, () => setPreview(undefined));
  const change=(next:Rule)=>{set(next);setPreview(undefined);setError('');};
  const numeric=counters.has(rule.event), channel=rule.event==='channel.subscribers';
  const durationAllowed=!channel && rule.event!=='video.unavailable' && rule.event!=='video.restored';
  const videoType = rule.video_type ?? formats[0];
  const formatAvailable = channel || (videoType !== undefined && formats.includes(videoType));
  async function submit(save:boolean) {
    if (!formatAvailable || busy) return;
    setBusy(true);setError('');
    try {
      const definition={...rule};
      if (!numeric) { delete definition.direction; delete definition.threshold; }
      if (channel) delete definition.video_type;
      else definition.video_type = videoType;
      if (!durationAllowed) { delete definition.min_duration; delete definition.max_duration; }
      if (save) {
        if (stored) await write('/rules/'+stored.id,{version:stored.version,rule:definition},'PATCH');
        else await write('/rules',{id,rule:definition});
        onSaved();
      } else {
        const v=await write<{context:{valid_before:string;evaluated_at:string}; rows:{id:string;cells:Record<string,{value:unknown}>}[]}>('/preview',definition);
        const labels=v.rows.map(r=>String(r.cells[channel?'channel.title':'video.title']?.value ?? r.id)
          + (numeric ? ' · '+String(r.cells[rule.event]?.value ?? 'unknown') : ''));
        setPreview(transferSourceLifetime(v,{labels,until:Date.parse(v.context.valid_before),at:v.context.evaluated_at}));
      }
    } catch(e) { setError(errorText(e)); } finally {setBusy(false);}
  }
  return <form className="alerts-editor" onSubmit={e=>{e.preventDefault();void submit(true);}}>
    <fieldset disabled={busy}><legend>{stored?'Edit alert':'Create alert'}</legend>
      <label>Name<input required maxLength={100} value={rule.name} onChange={e=>change({...rule,name:e.target.value})}/></label>
      {locked ? <p>{rule.scope.kind} · {rule.scope.id} · {rule.video_type}
        {rule.min_duration != null && ' · at least '+rule.min_duration+' s'}
        {rule.max_duration != null && ' · up to '+rule.max_duration+' s'}</p>
        : <><label>Watch<select value={rule.scope.kind} onChange={e=>change({...rule,scope:{kind:e.target.value as ScopeKind,id:''}})}>
        {(['channel','folder','label','video'] as const).filter(k=>!channel||k!=='video').map(k=><option key={k}>{k}</option>)}</select></label>
        <ScopeSelect scope={rule.scope} onChange={scope=>change({...rule,scope})}/>
        {!channel && <label>Format<select value={videoType ?? ''} onChange={e=>change({...rule,video_type:e.target.value as Rule['video_type']})}>
          {videoType && !formats.includes(videoType) && <option value={videoType} disabled>{videoType==='short'?'Shorts':'Long-form'} (unavailable)</option>}
          {formats.map(k=><option key={k} value={k}>{k==='short'?'Shorts':'Long-form'}</option>)}</select></label>}</>}
      <label>Notify me about<select value={rule.event} onChange={e=>{
        const event=e.target.value as EventName;
        change({...rule,event, ...(event==='channel.subscribers' && rule.scope.kind==='video'?{scope:{kind:'channel',id:''}}:{}),
          direction:rule.direction??'up',threshold:rule.threshold??'100000'});
      }}>{(Object.keys(eventNames) as EventName[]).filter(k=>{
        if (!locked) return true;
        if (k==='channel.subscribers') return false;
        return (initial.min_duration==null && initial.max_duration==null) || (k!=='video.unavailable' && k!=='video.restored');
      }).map(k=><option key={k} value={k}>{eventNames[k]}</option>)}</select></label>
      {!locked && durationAllowed && <div className="alerts-duration">
        <label>Minimum duration (seconds)<input type="number" min="0" step="1" value={rule.min_duration??''}
          onChange={e=>change({...rule,min_duration:e.target.value===''?undefined:e.target.valueAsNumber})}/></label>
        <label>Maximum duration (seconds)<input type="number" min={rule.min_duration??0} step="1" value={rule.max_duration??''}
          onChange={e=>change({...rule,max_duration:e.target.value===''?undefined:e.target.valueAsNumber})}/></label>
      </div>}
      {numeric && <><label>Direction<select value={rule.direction??'up'} onChange={e=>change({...rule,direction:e.target.value as Rule['direction']})}>
        <option value="up">Reaches or rises above</option><option value="down">Reaches or falls below</option></select></label>
        <label>Threshold<input required inputMode="numeric" pattern="0|[1-9][0-9]*" value={rule.threshold??''} onChange={e=>change({...rule,threshold:e.target.value})}/></label></>}
      {numeric && <p>Preview shows current values. An item already past the threshold must cross it again to notify you.</p>}
      <button type="button" disabled={!formatAvailable} onClick={()=>void submit(false)}>{numeric?'Preview values at or beyond threshold':'Preview watched videos'}</button>
      <button type="submit" disabled={!formatAvailable}>Save rule</button><button type="button" onClick={onClose}>Cancel</button>
    </fieldset>
    {preview && !sourceExpired(preview) && preview.until>Date.now() && <div><p>Current sample · {fmt(preview.at)}</p>
      {preview.labels.length?<ul>{preview.labels.map((l,i)=><li key={i}>{l}</li>)}</ul>:<p>No current matches.</p>}</div>}
    {error && <p role="alert">{error}</p>}
  </form>;
}

function ScopeSelect({scope,onChange}:{scope:Rule['scope'];onChange:(v:Rule['scope'])=>void}) {
  const [items,setItems]=useState<{id:string;name:string}[]>([]), [search,setSearch]=useState(''),[error,setError]=useState('');
  const searchable=scope.kind==='folder'||scope.kind==='label';
  useEffect(()=>{
    if(!searchable) return;
    const controller=new AbortController();setItems([]);setError('');
    void requestJson('/research/sources?kind='+scope.kind+'&limit=50&search='+encodeURIComponent(search),controller.signal)
      .then(v=>{if(!controller.signal.aborted)setItems((v as {items:{id:string;name:string}[]}).items);})
      .catch(e=>{if(!controller.signal.aborted)setError(errorText(e));});
    return ()=>controller.abort();
  },[scope.kind,searchable,search]);
  if(!searchable) return <label>{scope.kind==='video'?'Video ID':'Channel ID'}<input required value={scope.id}
    onChange={e=>onChange({...scope,id:e.target.value.trim()})}/></label>;
  return <><label>Find {scope.kind}<input value={search} onChange={e=>setSearch(e.target.value)}/></label>
    <label>{scope.kind}<select required value={scope.id} onChange={e=>onChange({...scope,id:e.target.value})}>
      <option value="">Choose…</option>{scope.id&&!items.some(i=>i.id===scope.id)&&<option>{scope.id}</option>}
      {items.map(i=><option key={i.id} value={i.id}>{i.name}</option>)}</select></label>{error&&<p role="alert">{error}</p>}</>;
}

export function WatchSelection({query}:{query:TranslatedQuery}) {
  const [open,setOpen]=useState(false),[message,msg]=useState('');
  let target:Selection;
  try {
    target=selection(query);
  } catch(e) {
    return <div><button type="button" disabled>Watch</button> {errorText(e)}</div>;
  }
  return <div><button type="button" onClick={()=>{setOpen(!open);msg('');}}>Watch</button> {message}
    {open&&<Editor initial={{...target,name:'Video views',event:'video.views',direction:'up',threshold:'100000'}}
      locked onClose={()=>setOpen(false)} onSaved={()=>{setOpen(false);msg('Rule saved. Future changes will appear in Alerts.');}}/>}</div>;
}
