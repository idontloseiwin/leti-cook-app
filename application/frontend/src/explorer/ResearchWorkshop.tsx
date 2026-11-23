import { useEffect,useRef,useState,type ReactNode } from 'react';
import { ExplorerApiError,requestJson } from './api';
import { ResearchConditions,firstCondition } from './ResearchConditions';
import { ResearchColumnsPicker } from './ResearchControls';
import { ResearchBlockResult,ResearchSourcePicker,localZone,offeredFields,studyFieldIssue,type CollectionsState,type ReadMode,type SourceChoices } from './WorkshopBlocks';
import { researchError,loadResearchCapabilities,channelSource,validScope,type ResearchCapabilities,type Expression,type ResearchSource } from './researchQuery';
import { parseResearchDefinition,parseGraph,researchDefinition,openResearch,researchTemplate,runResearch,withScope,withSourceQuery,studyForRows,studyProblems,templateOperations,
  TEMPLATE_NAMES,VIDEO_TEMPLATES,CHANNEL_TEMPLATES,withGalleryImage,type ResearchDocument,type NodeKind,type GraphResult,type GraphNode,type RunOptions,type StudyTemplate } from './researchGraph';
import { createSavedView,updateSavedResearch,type SavedView,type SavedViewCreate } from './savedViews';
import { canonicalSaved } from './savedQueryDefinition';
import { newSavedWorkId,savedName } from './savedWork';
import { WORKSHOP_BLOCKS,blockLabel,isOutputBlock,offeredBlocks,startHistory,recordEdit,undoEdit,redoEdit,shortcutAction,fixedReferenceOf,fixedListLine,studyError,type EditHistory } from './workshopModel';
import { EXPIRY_CHOICES,fixedListDecision,fixedListRefusal,type ExpiryChoice,type FixedListReference } from './workshopHandoff';
import './researchWorkshop.css';

export type StudyState={document:ResearchDocument;name:string;saved?:SavedView};
// What changes the study's rows (and so clears results): the root query and the blocks with their connections.
// Layout and notes are user work only.
const semanticKey=(d:ResearchDocument)=>canonicalSaved({query:d.query,nodes:d.graph.nodes,edges:d.graph.edges});
const queryKey=(d:ResearchDocument)=>canonicalSaved(d.query);
const scopeName=(kind:string)=>kind[0].toUpperCase()+kind.slice(1);
const templateNotice:Partial<Record<StudyTemplate,string>>={compare_channels:'Tick two to four channels in the table, then keep them as a comparison.',
  overview:'Calculated over every row of this selection, before pagination.'};
// The workshop page passes the study's tab-history hooks.
export function ResearchWorkshop({initial,initialSaved,initialName,initialNotice='',autoRun=false,fromSavedQuery=false,apiAvailable=true,refreshKey=0,
  initialRun,initialReference,onDocumentChange,onNewStudy,pageActions,sources,sourcesLoaded=false,sourceMissing=false,collections}:{initial:ResearchDocument;initialSaved?:SavedView;initialName?:string;
  initialNotice?:string;autoRun?:boolean;fromSavedQuery?:boolean;apiAvailable?:boolean;refreshKey?:number;
  // How the first run reads rows; a Library hand-off brings its fixed list (kept in this tab's memory only).
  initialRun?:RunOptions;initialReference?:FixedListReference;
  onDocumentChange?:(study:StudyState)=>void;
  onNewStudy?:(document:ResearchDocument,name:string,notice:string)=>void;pageActions?:ReactNode;sources?:SourceChoices;
  // The sidebar lists are loaded: a folder or label missing from them was deleted.
  sourcesLoaded?:boolean;sourceMissing?:boolean;collections?:CollectionsState}){
  const [history,setHistory]=useState<EditHistory<ResearchDocument>>(()=>startHistory(initial)),document=history.present;
  const [saved,setSaved]=useState(initialSaved),[name,setName]=useState(initialName??initialSaved?.name??'My research');
  const [mode,setMode]=useState<'template'|'canvas'>('template'),[selected,setSelected]=useState(initial.graph.nodes[0].id);
  const [result,setResult]=useState<{value:GraphResult;token:number;fromLibrary:boolean}|null>(null);
  const [busy,setBusy]=useState(false),[saving,setSaving]=useState(false),[message,setMessage]=useState(initialNotice),[caps,setCaps]=useState<ResearchCapabilities|null>(null);
  // Rows of this study: current reads, or one fixed list. The list's reference lives in memory only.
  const [readMode,setReadMode]=useState<ReadMode>(initialRun?.consistency??'current');
  const [reference,setReference]=useState<{value:FixedListReference;library:boolean}|undefined>(()=>initialReference?{value:initialReference,library:true}:undefined);
  const [choice,setChoice]=useState('');
  const mirror=useRef(onDocumentChange);mirror.current=onDocumentChange;
  useEffect(()=>{mirror.current?.({document,name,saved});},[document,name,saved]);
  const validation=useRef<AbortController|null>(null),runs=useRef(0);
  const request=useRef<AbortController|null>(null),createCommand=useRef<SavedViewCreate|undefined>(undefined);
  const drag=useRef<{id:string;x:number;y:number;startX:number;startY:number;key:string}|null>(null),drags=useRef(0);
  const rootQuery=document.query,graph=document.graph,node=graph.nodes.find(n=>n.id===selected)??graph.nodes[0];
  const channel=rootQuery.source.id===channelSource,zone=rootQuery.time?.timezone??'UTC';
  // A study runs and saves only with a chosen source and within the server's rules; Collection fields changes (refreshKey) re-read capabilities and clear results.
  const sourceChosen=validScope(rootQuery.scope),problems=studyProblems(document),fieldIssue=studyFieldIssue(document,caps);
  const runnable=sourceChosen&&apiAvailable&&!problems.length&&!fieldIssue;
  useEffect(()=>{const controller=new AbortController();setCaps(null);if(refreshKey)setResult(null);loadResearchCapabilities(controller.signal,rootQuery.source.id).then(value=>{if(!controller.signal.aborted)setCaps(value);}).catch(error=>{if(!controller.signal.aborted)setMessage(researchError(error));});return()=>controller.abort();},[rootQuery.source.id,refreshKey]);
  useEffect(()=>()=>{request.current?.abort();validation.current?.abort();},[]);
  const stop=()=>{request.current?.abort();request.current=null;setBusy(false);};
  // Every edit is one Undo step. A change of rows stops a run and clears results; a change of the root query
  // also drops the fixed list's reference (its list belongs to the old query).
  function settle(previous:ResearchDocument,next:ResearchDocument){
    validation.current?.abort();
    if(semanticKey(previous)!==semanticKey(next)){stop();setResult(null);setChoice('');}
    if(queryKey(previous)!==queryKey(next))setReference(undefined);
  }
  function change(next:ResearchDocument,key?:string){settle(document,next);setHistory(h=>recordEdit(h,next,key));setMessage('');}
  function step(direction:'undo'|'redo'){const next=(direction==='undo'?undoEdit:redoEdit)(history);if(next===history)return;settle(history.present,next.present);setHistory(next);setMessage('');}
  const stepRef=useRef(step);stepRef.current=step;
  useEffect(()=>{const onKey=(event:KeyboardEvent)=>{const action=shortcutAction({...event,target:event.target as HTMLElement|null});if(action){event.preventDefault();stepRef.current(action);}};
    window.addEventListener('keydown',onKey);return()=>window.removeEventListener('keydown',onKey);},[]);
  function parameters(value:Record<string,unknown>){change({...document,graph:{...graph,nodes:graph.nodes.map(n=>n.id===node.id?{...n,parameters:value}:n)}});}
  async function run(picked?:ExpiryChoice){
    let options:RunOptions={consistency:'current'};
    if(picked){options=picked==='fix_new_list'?{consistency:'frozen'}:{consistency:'current'};setReadMode(options.consistency);setReference(undefined);}
    else if(readMode==='frozen'){
      const decision=reference?fixedListDecision(reference.value,Date.now()):null;
      // Library's list expired before this run: the user chooses; a list this study fixed itself is fixed again.
      if(decision?.action==='choose'&&reference?.library){stop();setResult(null);setChoice(decision.notice);return;}
      options=decision?.action==='reuse'?decision.run:{consistency:'frozen'};
    }
    stop();setResult(null);setMessage('');setChoice('');const controller=new AbortController();request.current=controller;setBusy(true);const timeout=setTimeout(()=>controller.abort(),36000);
    const usedLibrary=Boolean('frozen_ref' in options&&options.frozen_ref&&reference?.library);
    try{parseGraph(graph);const value=await runResearch(document,controller.signal,options);
      if(request.current===controller&&!controller.signal.aborted){
        const fixed=fixedReferenceOf(value),fromLibrary=usedLibrary&&Boolean(value.run?.fixed?.reused);
        setReference(fixed?{value:fixed,library:fromLibrary}:undefined);setResult({value,token:++runs.current,fromLibrary});
      }}
    catch(error){if(request.current===controller){
      const refusal=options.consistency==='frozen'&&error instanceof ExplorerApiError?fixedListRefusal(error.code):null;
      // The list is gone or belongs elsewhere: never replaced silently.
      if(refusal&&!controller.signal.aborted){setReference(undefined);setChoice(refusal.notice);}
      else setMessage(controller.signal.aborted?'Research stopped.':studyError(error));}}
    finally{clearTimeout(timeout);if(request.current===controller)setBusy(false);}
  }
  // One run on arrival (a Saved query or a Library hand-off), once the source and capabilities are ready.
  const autoRequested=useRef(false);
  useEffect(()=>{if(autoRun&&!autoRequested.current&&caps&&runnable){autoRequested.current=true;void run();}},[autoRun,caps,runnable]);
  // A folder or label that left the loaded sidebar was deleted: the choice is cleared, the blocks stay.
  // (Collections are paged; channels can be missing while their sidebar entry refreshes.)
  useEffect(()=>{const scope=rootQuery.scope;
    if(!sourcesLoaded||!sources||!scope.id||(scope.kind!=='folder'&&scope.kind!=='label')||scope.id==='monetized'||sources[scope.kind].some(c=>c.id===scope.id))return;
    change(withScope(document,{kind:scope.kind,id:''}));setMessage('The selected source is no longer available.');
  },[sourcesLoaded,sources,rootQuery.scope.kind,rootQuery.scope.id]);
  function startNew(next:ResearchDocument,notice:string){if(onNewStudy){stop();onNewStudy(next,name,notice);return;}
    settle(document,next);setHistory(startHistory(next));setSaved(undefined);createCommand.current=undefined;setSelected(next.graph.nodes[0].id);setMessage(notice);}
  async function check(){validation.current?.abort();const controller=new AbortController();validation.current=controller;const timer=setTimeout(()=>controller.abort(),10000);
    try{await requestJson('/research/graphs/validate',controller.signal,{method:'POST',headers:{'Content-Type':'application/json'},body:canonicalSaved(document)});if(!controller.signal.aborted)setMessage('Connections, fields and permissions are valid.');}
    catch(error){if(!controller.signal.aborted)setMessage(researchError(error, 'Check connections again.'));}finally{clearTimeout(timer);}
  }
  async function save(){setSaving(true);setMessage('');try{
    const definition=researchDefinition(document),normalizedName=savedName(name);
    let value:SavedView;
    // A changed channel binding cannot update the same Saved item; it is saved
    // as a new study with the same blocks, notes and layout, the old one stays.
    if(saved&&saved.channel_id===definition.channel_id)value=await updateSavedResearch(saved,normalizedName,definition.conditions);
    else{const next={name:normalizedName,...definition};if(!createCommand.current||canonicalSaved({...createCommand.current,id:undefined})!==canonicalSaved(next))createCommand.current={id:newSavedWorkId(),...next};value=await createSavedView(createCommand.current);}
    const separate=Boolean(saved&&saved.channel_id!==definition.channel_id),copied=!saved&&fromSavedQuery;
    // Only a v3 study can be edited in Saved: a study opened from a saved query is saved as a new one.
    setSaved(value);setMessage(separate?'The source channel changed, so this was saved as a new study; the previous one is kept. Results were not stored.'
      :copied?'Saved as a new study. The original saved query is kept. Results were not stored.':'Research saved. Results were not stored.');
    // Keep this entry's history state (the study mirror); only the address gains ?saved=.
    const url=new URL(window.location.href);url.searchParams.set('saved',value.id);window.history.replaceState(window.history.state,'',url);
  }catch(error){setMessage(error instanceof Error?error.message:'Research could not be saved.');}finally{setSaving(false);}}
  const newId=(taken:Set<string>)=>'block_'+Array.from({length:100},(_,i)=>i+1).find(i=>!taken.has('block_'+i));
  const limited=(add:number,outputs:number)=>graph.nodes.length+add>12||graph.nodes.filter(n=>isOutputBlock(n.type)).length+outputs>4;
  function add(type:NodeKind){if(limited(1,isOutputBlock(type)?1:0)){setMessage('This study is limited to 12 blocks and 4 result blocks.');return;}
    const id=newId(new Set(graph.nodes.map(n=>n.id)));
    const parent=isOutputBlock(node.type)?graph.nodes.find(n=>n.type==='source')!:node;
    change({...document,graph:{...graph,nodes:[...graph.nodes,{id,type,parameters:WORKSHOP_BLOCKS[type].defaults(zone)}],edges:[...graph.edges,{from:parent.id,to:id,input:'query',output:'query'}],layout:{...graph.layout,[id]:{x:Math.min(10000,graph.layout[parent.id].x+280),y:Math.min(10000,graph.layout[parent.id].y+graph.nodes.length*25)}}}});setSelected(id);
  }
  /** "Keep as comparison": Selected IDs (these rows) → a table, shown side by side; saved with the study. */
  function keepComparison(ids:string[]){if(limited(2,1)){setMessage('This study is limited to 12 blocks and 4 result blocks.');return;}
    const taken=new Set(graph.nodes.map(n=>n.id)),pick=newId(taken);taken.add(pick);const table=newId(taken),source=graph.nodes.find(n=>n.type==='source')!;
    const y=Math.min(10000,Math.max(...Object.values(graph.layout).map(p=>p.y))+160),nodes:GraphNode[]=[{id:pick,type:'selection',parameters:{mode:'include',ids}},{id:table,type:'table',parameters:{}}];
    change({...document,graph:{...graph,nodes:[...graph.nodes,...nodes],edges:[...graph.edges,{from:source.id,to:pick,input:'query',output:'query'},{from:pick,to:table,input:'query',output:'query'}],
      layout:{...graph.layout,[pick]:{x:304,y},[table]:{x:584,y}}}});setSelected(table);setMessage(`Comparison kept: ${ids.length} ${channel?'channels':'videos'} in a Selected IDs block and a side-by-side table. Run research to read them.`);
  }
  function remove(){const removed=new Set([node.id]);for(let i=0;i<12;i++)for(const e of graph.edges)if(removed.has(e.from))removed.add(e.to);
    change({...document,graph:{...graph,nodes:graph.nodes.filter(n=>!removed.has(n.id)),edges:graph.edges.filter(e=>!removed.has(e.from)&&!removed.has(e.to)),layout:Object.fromEntries(Object.entries(graph.layout).filter(([id])=>!removed.has(id)))}});setSelected(graph.nodes.find(n=>n.type==='source')!.id);
  }
  function connect(parent:string){const next={...graph,edges:graph.edges.map(e=>e.to===node.id?{...e,from:parent}:e)};
    // Allow unfinished parameters while editing, but never a cycle.
    let current=parent;const seen=new Set([node.id]);while(current){if(seen.has(current)){setMessage('This connection would create a cycle.');return;}seen.add(current);current=next.edges.find(e=>e.to===current)?.from??'';}
    change({...document,graph:next});
  }
  // Dragging a block is one Undo step; so is a run of arrow-key nudges of one block.
  function position(id:string,x:number,y:number,key:string){setHistory(h=>recordEdit(h,{...h.present,graph:{...h.present.graph,layout:{...h.present.graph.layout,[id]:{x:Math.max(0,Math.min(10000,Math.round(x))),y:Math.max(0,Math.min(10000,Math.round(y)))}}}},key));}
  // A block shown as a gallery brings the row image into the study's columns (shared by every row block).
  function retype(type:'table'|'gallery'){if(node.type!==type)change({...document,query:type==='gallery'?withGalleryImage(document.query):document.query,graph:{...graph,nodes:graph.nodes.map(n=>n.id===node.id?{...n,type}:n)}});}
  async function importFile(file:File){if(file.size>20000){setMessage('Research files are limited to 20 KB.');return;}try{const raw=JSON.parse(await file.text());const definition=parseResearchDefinition(raw.definition);if(raw.format!=='leti-cook-research'||raw.version!==1||Object.keys(raw).sort().join()!=='channel_id,definition,format,version')throw new Error('Unsupported research file.');startNew(openResearch(definition,raw.channel_id),'Definition imported. Running it will check current access and available fields.');}catch{setMessage('The file is not a supported research definition.');}}
  function exportFile(){try{const definition=researchDefinition(document),blob=new Blob([canonicalSaved({format:'leti-cook-research',version:1,channel_id:definition.channel_id,definition:definition.conditions})],{type:'application/json'}),url=URL.createObjectURL(blob);const link=window.document.createElement('a');link.href=url;link.download='leti-cook-research.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(error){setMessage(error instanceof Error?error.message:'Export failed.');}}
  function setRows(source:ResearchSource){startNew(studyForRows(document,source),source===channelSource?'Channel rows: Channel list on the same source.':'Video rows: Latest uploads on the same source.');}
  function setRead(next:ReadMode){if(next===readMode)return;stop();setResult(null);setChoice('');setReadMode(next);}
  const live=reference&&Date.parse(reference.value.valid_before)>Date.now();
  const modeNote=readMode==='current'?'Every block reads current data; pages can move while you page.'
    :reference&&live?fixedListLine(reference.value,reference.library,channel)+'. Run reuses this list.'
    :reference?.library?fixedListDecision(reference.value,Date.now()).notice
    :reference?'The fixed list expired. Run fixes a new list (counts may differ).'
    :'Run fixes one list of the source rows for 10 minutes. Tables and galleries fed by the source show it; other blocks read current data.';
  const scopeChoice=sources?.[rootQuery.scope.kind].find(c=>c.id===rootQuery.scope.id)?.name??(rootQuery.scope.id||'not chosen');
  const templates=(channel?CHANNEL_TEMPLATES:VIDEO_TEMPLATES).filter(t=>{const ops=templateOperations(t);return !ops.length||ops.some(op=>caps?.operations.includes(op));});
  const offered=offeredBlocks(channel?'channel':'video',caps?.operations??[]),fields=offeredFields(document,caps);
  const width=Math.max(900,...Object.values(graph.layout).map(p=>p.x+270)),height=Math.max(450,...Object.values(graph.layout).map(p=>p.y+130));
  const current=result?.value;
  const stateOf=(id:string)=>current?.nodes[id]?(current.nodes[id].state==='error'?'Error':current.nodes[id].consistency==='frozen'?'Fixed list':'Ready'):'Not run';
  return <section className="research-workshop"><header><span className="explorer-eyebrow">Research workshop</span><h1>{name||'Untitled research'}</h1><p>Build a study from stored YouTube data. Fetched data stays current; your questions, selections and notes stay yours.</p></header>
    {pageActions}
    <div className="research-actions"><label>Name <input maxLength={120} value={name} onChange={e=>setName(e.target.value)}/></label><button disabled={saving||!sourceChosen||problems.length>0} onClick={()=>void save()}>{saving?'Saving…':saved?'Save changes':'Save research'}</button><button onClick={exportFile}>Export definition</button><label className="research-file">Import definition<input type="file" accept="application/json,.json" onChange={e=>{const file=e.target.files?.[0];if(file)void importFile(file);e.target.value='';}}/></label></div>
    <div className="research-actions" role="group" aria-label="Research view"><button aria-pressed={mode==='template'} onClick={()=>setMode('template')}>Template view</button><button aria-pressed={mode==='canvas'} onClick={()=>setMode('canvas')}>Canvas</button><button disabled={busy||!runnable} onClick={()=>void run()}>Run research</button>{busy&&<button onClick={()=>{stop();setMessage('Research stopped.');}}>Stop</button>}
      <button onClick={()=>void check()}>Check connections</button>
      <button disabled={!history.past.length} title="Undo the last change (Ctrl+Z)" onClick={()=>step('undo')}>Undo</button><button disabled={!history.future.length} title="Redo (Ctrl+Shift+Z)" onClick={()=>step('redo')}>Redo</button></div>
    {choice&&<div className="research-message workshop-choice" role="group" aria-label="Fixed list choice"><p role="status">{choice}</p><div className="research-actions">{EXPIRY_CHOICES.map(c=><button key={c.id} onClick={()=>void run(c.id)}>{c.label}</button>)}</div></div>}
    <details><summary>Start from a template</summary><p>Starts a new study using this source.</p><div className="research-actions">{templates.map(t=><button key={t} onClick={()=>startNew(researchTemplate(rootQuery,t,caps?.operations),templateNotice[t]??'')}>{TEMPLATE_NAMES[t]}</button>)}</div></details>
    <p className="research-note">Source: {scopeName(rootQuery.scope.kind)} · {scopeChoice} · {channel?'channels':rootQuery.video_types?.[0]==='short'?'Shorts':rootQuery.video_types?.[0]==='stream'?'completed Streams':'Long-form'} · {readMode==='frozen'?'fixed list':'current reads'}. Up to 12 blocks and 4 result blocks; no stored result snapshot.</p>
    {!sourceChosen&&<p role="status">Choose a source in the Source block to run or save this study.</p>}{!apiAvailable&&<p role="status">Stored data is temporarily unavailable.</p>}
    {problems.map(problem=><p role="status" key={problem}>{problem}</p>)}{fieldIssue&&<p role="status">{fieldIssue}</p>}
    <div className="research-actions"><label>Add block <select defaultValue="" onChange={e=>{if(e.target.value)add(e.target.value as NodeKind);e.target.value='';}}><option value="">Choose…</option>{offered.map(type=><option key={type} value={type}>{blockLabel(type)}</option>)}</select></label><button onClick={()=>change({...document,graph:{...graph,layout:Object.fromEntries(graph.nodes.map((n,i)=>[n.id,{x:24+i%3*280,y:36+Math.floor(i/3)*160}]))}})}>Arrange blocks</button></div>
    <div className="workshop-editors">{mode==='canvas'?<div className="workshop-canvas-scroll"><div className="workshop-canvas" style={{width,height}}>
      <svg width={width} height={height} aria-label="Research block connections" role="img">{graph.edges.map(e=>{const a=graph.layout[e.from],b=graph.layout[e.to];return <path key={e.to} d={`M ${a.x+240} ${a.y+48} C ${a.x+280} ${a.y+48}, ${b.x-40} ${b.y+48}, ${b.x} ${b.y+48}`} fill="none" stroke="#63d8b4" strokeWidth="2"/>;})}</svg>
      {graph.nodes.map(n=><article className={'workshop-node'+(n.id===node.id?' selected':'')} key={n.id} style={{left:graph.layout[n.id].x,top:graph.layout[n.id].y}}>
        <button className="workshop-node-handle" aria-label={'Move or select '+blockLabel(n.type)} onClick={()=>setSelected(n.id)} onKeyDown={e=>{const delta=({ArrowLeft:[-10,0],ArrowRight:[10,0],ArrowUp:[0,-10],ArrowDown:[0,10]} as Record<string,number[]>)[e.key];if(delta){e.preventDefault();position(n.id,graph.layout[n.id].x+delta[0],graph.layout[n.id].y+delta[1],'nudge:'+n.id);}}}
          onPointerDown={e=>{if(e.button!==0)return;setSelected(n.id);drag.current={id:n.id,x:graph.layout[n.id].x,y:graph.layout[n.id].y,startX:e.clientX,startY:e.clientY,key:'move:'+n.id+':'+(++drags.current)};e.currentTarget.setPointerCapture(e.pointerId);}}
          onPointerMove={e=>{const d=drag.current;if(d&&d.id===n.id&&(e.clientX!==d.startX||e.clientY!==d.startY))position(n.id,d.x+e.clientX-d.startX,d.y+e.clientY-d.startY,d.key);}}
          onPointerUp={()=>{drag.current=null;}} onPointerCancel={()=>{drag.current=null;}}>{blockLabel(n.type)}</button><small>{n.id} · {stateOf(n.id)}</small><button onClick={()=>setSelected(n.id)}>Edit block</button>
      </article>)}
    </div></div>:<ol className="workshop-block-list">{graph.nodes.map(n=><li key={n.id}><button aria-pressed={selected===n.id} onClick={()=>setSelected(n.id)}>{blockLabel(n.type)}</button><small>{n.id} · {graph.edges.find(e=>e.to===n.id)?.from??'study source'} → {isOutputBlock(n.type)?'result':'query'} · {stateOf(n.id)}</small></li>)}</ol>}
    <aside className="workshop-inspector"><h2>{blockLabel(node.type)}</h2><p>{node.id}</p>{node.type!=='source'&&<label>Input query <select value={graph.edges.find(e=>e.to===node.id)?.from??''} onChange={e=>connect(e.target.value)}>{graph.nodes.filter(n=>!isOutputBlock(n.type)&&n.id!==node.id).map(n=><option key={n.id} value={n.id}>{blockLabel(n.type)} · {n.id}</option>)}</select></label>}
      {node.type==='source'&&<ResearchSourcePicker document={document} caps={caps} sources={sources} collections={collections} mode={readMode} modeNote={modeNote}
        notice={sourceMissing&&rootQuery.scope===initial.query.scope?'The saved source is no longer available. Choose another source; blocks, notes and layout are kept.':undefined}
        onChange={next=>change(next)} onRows={setRows} onMode={setRead}/>}
      {(node.type==='table'||node.type==='gallery')&&<><div className="research-actions" role="group" aria-label="Show rows as"><span>Show as</span><button type="button" aria-pressed={node.type==='table'} onClick={()=>retype('table')}>Table</button><button type="button" aria-pressed={node.type==='gallery'} onClick={()=>retype('gallery')}>Gallery</button></div>
        <details open><summary>Columns ({rootQuery.select.length})</summary><ResearchColumnsPicker columns={rootQuery.select} fields={fields} channels={channel} loaded={Boolean(caps)} onChange={select=>change(withSourceQuery(document,{select}))}/>
          <small>Columns are shared by every table and gallery of this study.</small></details></>}
      {node.type==='filter'&&(node.parameters.where&&caps?<ResearchConditions value={node.parameters.where as Expression} fields={fields} onChange={where=>parameters({where})}/>:<button disabled={!fields.length} onClick={()=>parameters({where:firstCondition(fields)})}>Add condition</button>)}
      {node.type==='selection'&&<><label>Selection <select value={String(node.parameters.mode)} onChange={e=>parameters({...node.parameters,mode:e.target.value})}><option value="include">Only these IDs</option><option value="exclude">Exclude these IDs</option></select></label><label>IDs, one per line<textarea rows={6} maxLength={25000} value={(node.parameters.ids as string[]).join('\n')} onChange={e=>parameters({...node.parameters,ids:e.target.value.split('\n').map(s=>s.trim()).filter(Boolean)})}/></label><p>Saved references only. Missing or expired source data is never reconstructed.</p></>}
      {node.type==='calendar'&&<><label>Year <input type="number" min={1} max={9999} value={Number(node.parameters.year)} onChange={e=>parameters({...node.parameters,year:Number(e.target.value)})}/></label>{'time_zone' in node.parameters
        ?<><label>Time zone <select value={String(node.parameters.time_zone)} onChange={e=>{const {time_zone:_saved,...rest}=node.parameters;parameters(e.target.value===''?rest:{...node.parameters,time_zone:e.target.value});}}><option value="">The study's zone ({document.query.time?.timezone??'UTC'})</option><option value="UTC">UTC</option>{localZone()!=='UTC'&&<option value={localZone()}>Local ({localZone()})</option>}{![ 'UTC',localZone()].includes(String(node.parameters.time_zone))&&<option value={String(node.parameters.time_zone)}>{String(node.parameters.time_zone)}</option>}</select></label><small>Saved with its own time zone. Choose the study's zone to count in Source › Dates and years.</small></>
        :<small>Counts days in the study's zone ({document.query.time?.timezone??'UTC'}), set in Source › Dates and years.</small>}</>}
      {node.type==='history'&&<><label>Metric <select value={String(node.parameters.metric)} onChange={e=>parameters({...node.parameters,metric:e.target.value})}><option value="views">Views</option><option value="subscribers">Subscribers</option></select></label>
        {rootQuery.scope.kind!=='channel'&&<p role="status">Channel history needs one channel. Choose a channel or remove the history block.</p>}</>}
      {node.type!=='source'&&<button onClick={remove}>Remove block and its children</button>}
    </aside></div>
    <label className="workshop-notes">My research notes<textarea rows={5} maxLength={4096} value={graph.notes} onChange={e=>change({...document,graph:{...graph,notes:e.target.value}},'notes')} placeholder="Your question, observations and links. Fetched results are not copied here."/></label>
    {busy&&<p role="status">Running the research…</p>}{message&&<p role="status">{message}</p>}
    {result&&current&&<section className="workshop-results"><h2>Results</h2><p>{current.status==='partial'?'Some blocks could not be completed.':'Research complete.'} {current.run?.fixed
      ?fixedListLine(current.run.fixed,result.fromLibrary,channel)+'. Blocks marked Fixed list show it; other blocks are current reads, each with its own read time.'
      :'Current reads; results may have different observation times.'}</p>
      {graph.nodes.filter(n=>isOutputBlock(n.type)).map(n=>{const output=current.nodes[n.id];return <section key={n.id}><h3>{blockLabel(n.type)} · {n.id}{output?.consistency==='frozen'?' · Fixed list':''}</h3>
        {output&&<ResearchBlockResult key={result.token+':'+n.id} id={n.id} type={n.type} document={document} result={output} fixedStudy={Boolean(current.run?.fixed)}
          onKeepComparison={n.type==='table'?keepComparison:undefined} onUseCurrentReads={()=>void run('use_current_reads')}/>}
        {output?.elapsed_ms!==undefined&&<small>{output.elapsed_ms} ms</small>}</section>;})}</section>}
  </section>;
}
