import { useState, useEffect } from 'react';
import { explorerChannelPath } from '../explorerRoutes';
import { requestJson } from './api';
import { useSourceExpiry } from '../sourceLifetimeReact';
import { sourceExpired, transferSourceLifetime } from '../sourceLifetime';
import { BASE, eventNames, errorText, fmt, type Rule, type Stored, Editor, write } from './AlertEditor';
type Notice = { id: string; rule_name: string; event: keyof typeof eventNames; direction?: string; threshold?: string;
  subject: string; channel_id: string; video_title?: string; can_open_video: boolean; video_type?: string; previous_value?: string; current_value?: string; previous_at?: string;
  published_at?: string; observed_at: string; source_expires_at: string; is_read: boolean };

export default function AlertsPage(){
  const [tab,setTab]=useState<'inbox'|'rules'>('inbox'),[rules,setRules]=useState<Stored[]>([]),[items,setItems]=useState<Notice[]>([]);
  const [processor,setProcessor]=useState(''),[cursor,setCursor]=useState(''),[next,setNext]=useState<string|null>(null);
  const [revision,refresh]=useState(0),[error,setError]=useState(''),[ready,setReady]=useState(false);
  const [editor,setEditor]=useState<{rule:Rule;stored?:Stored}>(),[removing,setRemoving]=useState('');
  useEffect(()=>{
    const controller=new AbortController();setError('');setReady(false);
    let loading=false;
    const load=async()=>{
      if(loading || controller.signal.aborted) return;
      loading=true;
      try {
      if(tab==='rules'){const v=await requestJson(BASE+'/rules',controller.signal) as {items:Stored[];processor:string};
        if(!controller.signal.aborted){setRules(v.items);setProcessor(v.processor);}}
      else{const v=await requestJson(BASE+'/inbox'+(cursor?'?before='+cursor:''),controller.signal) as {items:Notice[];next:string|null};
        if(!controller.signal.aborted){setItems(transferSourceLifetime(v,v.items));setNext(v.next);}}
      if(!controller.signal.aborted){setReady(true);setError('');}
      } catch(e) {if(!controller.signal.aborted)setError(errorText(e));}
      finally {loading=false;}
    };
    void load();
    const refreshInbox=()=>{if(!document.hidden)void load();};
    if(tab!=='inbox' || cursor) return ()=>controller.abort();
    const timer=window.setInterval(refreshInbox,15000);
    document.addEventListener('visibilitychange',refreshInbox);
    return ()=>{controller.abort();window.clearInterval(timer);document.removeEventListener('visibilitychange',refreshInbox);};
  },[tab,cursor,revision]);
  useSourceExpiry(items,()=>{ setReady(false); setItems([]); refresh(n=>n+1); });
  async function action(path:string,body:unknown,method='POST'){
    try{await write(path,body,method);refresh(n=>n+1);setRemoving('');}catch(e){setError(errorText(e));}
  }
  return <main className="alerts-page"><h1>Alerts</h1>
    <nav aria-label="Alert views"><button aria-pressed={tab==='inbox'} onClick={()=>{setTab('inbox');setCursor('');}}>Inbox</button>
      <button aria-pressed={tab==='rules'} onClick={()=>setTab('rules')}>Rules</button>
      <button onClick={()=>refresh(n=>n+1)}>Refresh</button></nav>
    {error&&<p role="alert">{error}</p>}{!ready&&!error&&<p>Loading…</p>}
    {tab==='rules'&&<><button onClick={()=>setEditor({rule:{name:'',scope:{kind:'channel',id:''},event:'video.views',direction:'up',threshold:'100000'}})}>Create rule</button>
      {editor&&<Editor key={editor.stored?.id??'new'} initial={editor.rule} stored={editor.stored} onClose={()=>setEditor(undefined)}
        onSaved={()=>{setEditor(undefined);refresh(n=>n+1);}}/>}
      {ready&&processor==='waiting'&&<p role="status">Observation processing is waiting. Pending observations will be retried.</p>}
      {ready&&!rules.length&&<p>No rules yet. Create one here or select Watch in Library.</p>}
      {ready&&rules.length>0&&<p>Pausing stops new observations. Messages from before the pause can still arrive.</p>}
      {ready&&rules.map(r=><article key={r.id}><strong>{r.definition.name}</strong> · {eventNames[r.definition.event]} · {r.enabled?'Watching':'Paused'}
        <p>{r.definition.scope.kind} · {r.definition.scope.id} {r.definition.threshold&&' · '+r.definition.direction+' '+r.definition.threshold}</p>
        {!r.source_available&&<p>The selected source is unavailable.</p>}
        {r.gap_count>0&&<p>Coverage gap · Some observations could not be verified.{r.last_gap_at && ' Last reported '+fmt(r.last_gap_at)+'.'}</p>}
        <button onClick={()=>setEditor({rule:r.definition,stored:r})}>Edit</button>
        <button onClick={()=>void action('/rules/'+r.id,{version:r.version,enabled:!r.enabled},'PATCH')}>{r.enabled?'Pause':'Resume'}</button>
        {removing===r.id?<><button onClick={()=>void action('/rules/'+r.id+'?version='+r.version,undefined,'DELETE')}>Delete rule and its messages</button>
        <button onClick={()=>setRemoving('')}>Cancel</button></>:<button onClick={()=>setRemoving(r.id)}>Delete</button>}</article>)}</>}
    {ready&&tab==='inbox'&&<>{!items.length&&<p>No observed changes on this page.</p>}
      {items.filter(i=>!sourceExpired(i) && Date.parse(i.source_expires_at)>Date.now()).map(i=><article key={i.id}>
        <strong>{i.rule_name}</strong> · {eventNames[i.event]}{i.threshold&&<> · {i.direction==='up'?'up to or above':'down to or below'} {i.threshold}</>}
        <p>{i.previous_at&&<>{i.previous_value} ({fmt(i.previous_at)}) → {i.current_value} · </>}
        {i.published_at&&<>Published {fmt(i.published_at)} · </>}Observed {fmt(i.observed_at)}</p>
        <p>{i.video_title || i.subject}</p><a href={explorerChannelPath(i.channel_id)}>Open channel</a>{i.can_open_video&&<> · <a href={'https://www.youtube.com/watch?v='+encodeURIComponent(i.subject)} target="_blank" rel="noreferrer">Open video</a></>}
        {!i.is_read&&<button onClick={()=>void action('/inbox/'+i.id+'/read',{})}>Mark read</button>}</article>)}
      {cursor&&<button onClick={()=>setCursor('')}>Newest</button>}{next&&<button onClick={()=>setCursor(next)}>Older messages</button>}</>}
  </main>;
}
