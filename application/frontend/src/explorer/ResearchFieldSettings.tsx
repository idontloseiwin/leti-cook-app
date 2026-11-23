import { useEffect, useRef, useState } from 'react';
import { requestJson } from './api';
import { optionalFields, parseFieldPreferences, fieldMode, type FieldPreferences, type OptionalField, type Mode } from './researchFieldPreferences';
const types=['long','short','stream'] as const;
const names={'video.description':'Description','video.paid_promotion':'Declared paid promotion'};
type State={revision:number;document:FieldPreferences;pending_import:FieldPreferences|null};
type Preview={expected_revision:number;document:FieldPreferences;storage:{stored_videos:number;retained_descriptions:number;sampled_description_bytes:number|null;optional_relation_bytes:number;estimated_description_payload_bytes:number|null;maximum_description_and_folded_bytes:number;estimate_basis:string};affected_saved:{id:string;name:string;fields:string[]}[]};
const headers={'Content-Type':'application/json','X-Saved-Work':'1'};
const mib=(n:number)=>(n/1048576).toLocaleString('en-US',{maximumFractionDigits:1})+' MiB';
function state(value:unknown):State{
 const row=value as Record<string,unknown>;if(!row||!Number.isSafeInteger(row.revision)||Number(row.revision)<1)throw new Error('Invalid field settings.');
 return {revision:row.revision as number,document:parseFieldPreferences(row.document),pending_import:row.pending_import===null?null:parseFieldPreferences(row.pending_import)};
}
// initialOpen: the workshop address ?settings=fields (the link from Library's advanced filters).
export function ResearchFieldSettings({changed,initialOpen=false}:{changed:()=>void;initialOpen?:boolean}){
 const [open,setOpen]=useState(initialOpen),[current,setCurrent]=useState<State|null>(null),[document,setDocument]=useState<FieldPreferences|null>(null),[attempt,setAttempt]=useState(0);
 const [estimate,setEstimate]=useState<Preview|null>(null),[purge,setPurge]=useState<OptionalField[]>([]),[busy,setBusy]=useState(false),[message,setMessage]=useState('');
 const pending=useRef<{document:FieldPreferences;expected_revision:number;request_id:string;purge:OptionalField[]}|null>(null);
 const active=useRef(true);
 useEffect(()=>{active.current=true;setMessage('');const controller=new AbortController();requestJson('/research/fields',controller.signal).then(value=>{
  if(controller.signal.aborted)return;const next=state((value as {preferences:unknown}).preferences);setCurrent(next);setDocument(next.document);
  if(next.document.description_setup==='pending'||next.pending_import)setOpen(true);
 }).catch(()=>{if(!controller.signal.aborted)setMessage('Field settings could not be loaded. Reopen Collection fields to retry.');});
 return()=>{active.current=false;controller.abort();};},[attempt]);
 const alter=(next:FieldPreferences)=>{setDocument(parseFieldPreferences(next));setEstimate(null);pending.current=null;setPurge([]);setMessage('');};
 const setMode=(field:OptionalField,kind:typeof types[number]|'workspace',value:string)=>{
  if(!document)return;const next=structuredClone(document);next.description_setup='chosen';
  if(kind==='workspace')next.workspace[field]=value as Mode;
  else {const source=next.sources['public.videos.current.v1']??={fields:{},types:{}};const choices=source.types[kind]??={};
   if(value==='inherit')delete choices[field];else choices[field]=value as Mode;}
  alter(next);
 };
 const preview=async()=>{if(!document)return;setBusy(true);setMessage('');try{
  const value=await requestJson('/research/fields/preview',undefined,{method:'POST',headers,body:JSON.stringify({document})}) as Preview;
  if(!active.current)return;if(!Number.isSafeInteger(value.expected_revision)||!value.storage||!Array.isArray(value.affected_saved))throw new Error('Invalid field estimate.');
  setEstimate(value);pending.current={document:parseFieldPreferences(value.document),expected_revision:value.expected_revision,request_id:crypto.randomUUID(),purge:[...purge]};
 }catch(error){if(active.current)setMessage(error instanceof Error?error.message:'Preview unavailable.');}finally{if(active.current)setBusy(false);}};
 const apply=async()=>{if(!pending.current)return;setBusy(true);setMessage('');try{
  const value=await requestJson('/research/fields/apply',undefined,{method:'POST',headers,body:JSON.stringify(pending.current)}) as {preferences:unknown};
  if(!active.current)return;const next=state(value.preferences);setCurrent(next);setDocument(next.document);setEstimate(null);pending.current=null;setPurge([]);
  setMessage('Settings saved. Current values arrive on their existing schedules. Requested deletion continues in bounded cleanup batches.');changed();
 }catch(error){if(active.current)setMessage((error instanceof Error?error.message:'Save could not be confirmed.')+' Retry uses the same command.');}finally{if(active.current)setBusy(false);}};
 // Opening the panel again after a failed read retries it.
 return <section className="research-field-settings"><button aria-expanded={open} onClick={()=>{if(!open&&!current&&message)setAttempt(n=>n+1);setOpen(!open);}}>Collection fields</button>
 {open&&<div><h2>Choose what to collect</h2><p>Descriptions enable text search. Declared paid promotion is a YouTube field; missing information is not “No”. Both start off.</p>
 <p>Identity, format evidence, availability, publication and source clocks stay required. Other existing fields keep their current collection behavior.</p>
 {current?.pending_import&&<p>Imported choices are saved but inactive. <button disabled={busy} onClick={()=>alter(current.pending_import!)}>Review imported choices</button></p>}
 {document&&<><div className="research-field-table"><table><thead><tr><th>Field</th><th>Workspace default</th>{types.map(kind=><th key={kind}>{kind==='long'?'Long-form':kind==='short'?'Shorts':'Streams'}</th>)}</tr></thead><tbody>{optionalFields.map(field=><tr key={field}><th>{names[field]}</th>
 <td><select aria-label={names[field]+' workspace default'} disabled={busy} value={document.workspace[field]??'off'} onChange={e=>setMode(field,'workspace',e.target.value)}><option value="off">Off</option><option value="current">Collect current value</option></select></td>
 {types.map(kind=><td key={kind}><select aria-label={names[field]+' '+kind} disabled={busy} value={document.sources['public.videos.current.v1']?.types[kind]?.[field]??'inherit'} onChange={e=>setMode(field,kind,e.target.value)}><option value="inherit">Inherit ({fieldMode(document,kind,field)})</option><option value="off">Off</option><option value="current">Collect current value</option></select></td>)}</tr>)}</tbody></table></div>
 <p>Off stops storing and showing the field. Description shares the existing snippet response and is discarded when off; the paid-promotion part is omitted unless a format needs it. Re-enabling cannot reconstruct missed history.</p>
 {optionalFields.map(field=><label key={field}><input type="checkbox" disabled={busy||types.some(kind=>fieldMode(document,kind,field)!=='off')} checked={purge.includes(field)} onChange={e=>{setPurge(value=>e.target.checked?[...value,field]:value.filter(item=>item!==field));setEstimate(null);pending.current=null;}}/> Delete retained {names[field].toLowerCase()} now in bounded batches (all formats must be off)</label>)}
 <p>Otherwise retained values are removed automatically after 29 days. Turning a field off never stops expiry cleanup.</p>
 <button disabled={busy} onClick={()=>void preview()}>Preview changes and storage</button>
 {document.description_setup==='pending'&&<button disabled={busy} onClick={()=>alter({...document,description_setup:'later'})}>Decide later (preview and apply)</button>}
 {estimate&&<div><p>{estimate.storage.stored_videos.toLocaleString()} stored videos. Optional table and indexes currently use {mib(estimate.storage.optional_relation_bytes)}.</p>
 <p>{estimate.storage.estimated_description_payload_bytes===null?'No stored sample yet.':'Description text estimate from up to 1,000 stored values: '+mib(estimate.storage.estimated_description_payload_bytes)+'.'} Maximum description plus search text: {mib(estimate.storage.maximum_description_and_folded_bytes)} before row/index overhead and WAL. Actual size depends on content; this is not a reservation.</p>
 <p>{estimate.affected_saved.length} saved queries may be blocked. Their definitions remain saved.</p>{estimate.affected_saved.length>0&&<ul>{estimate.affected_saved.map(row=><li key={row.id}>{row.name}: {row.fields.join(', ')}</li>)}</ul>}
 <p>No YouTube request or historical backfill starts when you apply. Metadata refreshes fill current values on the existing schedule.</p><button disabled={busy} onClick={()=>void apply()}>Apply reviewed settings</button></div>}
 </>}{message&&<p role="status">{message}</p>}</div>}</section>;
}
