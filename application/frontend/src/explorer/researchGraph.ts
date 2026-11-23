import { requestJson } from './api';
import { savedRecord } from './savedWork';
import { parseSavedDefinition,canonicalSaved,type SavedQuery,type SavedQueryDefinition,savedQueryFilters } from './savedQueryDefinition';
import { parseResearchPage,expressionFields,type ResearchPage,type ResearchFrozenPage,type ResearchFilters,type ResearchField,type ResearchSource,type Expression,channelSource,videoSource } from './researchQuery';
import { parseOperation,type OperationResult,type OperationId } from './researchOperations';
import { sourceExpired,sourceLifetime,transferSourceLifetime } from '../sourceLifetime';
import type { CanvasGraph } from './canvas/canvasModel';

export type NodeKind='source'|'filter'|'selection'|'table'|'gallery'|'summary'|'calendar'|'history';
export type GraphNode={id:string;type:NodeKind;parameters:Record<string,unknown>};
export type Graph={version:1;nodes:GraphNode[];edges:{from:string;to:string;input:'query';output:'query'}[];notes:string;layout:Record<string,{x:number;y:number}>};
export type ResearchDefinition={version:3;query:SavedQuery;graph:Graph};
export type ResearchDocument={version:1;query:SavedQuery;graph:Graph};
// How one run reads its rows: current reads, or one fixed list, reused while its reference lives.
// The reference is a run argument only: never part of the study, history, an export or Saved.
export type RunOptions={consistency:'current'}|{consistency:'frozen';frozen_ref?:string};
export type BlockPage=ResearchPage|ResearchFrozenPage;
export type NodeResult={state:'ready'|'error';output?:'query'|'page'|'operation';grain?:'video'|'channel';value?:BlockPage|OperationResult;code?:string;
  path?:string|null;elapsed_ms?:number;
  // A too-large refusal may name its bound ('rows' | 'memory' | 'time'); an unknown value is shown without one.
  limit?:string;
  // Sent with run options: how this block was read, and the query a table or gallery block pages with.
  consistency?:'current'|'frozen';query?:Record<string,unknown>};
export type FixedListRun={reused:boolean;evaluated_at:string;valid_before:string;total_count:number};
export type GraphResult={version:1;graph_fingerprint:string;context:{evaluated_at:string;valid_before:string;consistency:'sequential_current_reads'|'fixed_source_list'};
  status:'ready'|'partial';run?:{consistency:'current'|'frozen';fixed:FixedListRun|null};nodes:Record<string,NodeResult>};
/** Every block type the browser knows; the block registry (workshopBlocks.ts) describes each one. */
export const NODE_KINDS:readonly NodeKind[]=['source','filter','selection','table','gallery','summary','calendar','history'];
/** Result blocks: they show rows or a calculation and feed nothing. */
export const OUTPUT_KINDS:readonly NodeKind[]=['table','gallery','summary','calendar','history'];
export const ROW_KINDS:readonly NodeKind[]=['table','gallery'];
/** Calculation blocks and the server operation each one runs (server: graph.OPERATION_TYPES). */
export const OPERATION_OF_BLOCK={summary:'video.summary.v1',calendar:'video.publication_calendar.v1',history:'channel.observed_history.v1'} as const satisfies Partial<Record<NodeKind,OperationId>>;
const kinds=NODE_KINDS,sinks=OUTPUT_KINDS;
const fail=():never=>{throw new Error('Unsupported or invalid research definition.');};
function exact(v:unknown,keys:string[]){const o=savedRecord(v);if(Object.keys(o).sort().join()!==[...keys].sort().join())return fail();return o;}
export function parseGraph(value:unknown):Graph{
  const g=exact(value,['version','nodes','edges','notes','layout']);
  if(g.version!==1||!Array.isArray(g.nodes)||g.nodes.length<2||g.nodes.length>12||!Array.isArray(g.edges)||g.edges.length>11
    ||typeof g.notes!=='string'||g.notes.includes('\0')||new TextEncoder().encode(g.notes).length>4096)return fail();
  const ids=new Set<string>(),nodes:GraphNode[]=[];
  for(const raw of g.nodes){const n=exact(raw,['id','type','parameters']);if(typeof n.id!=='string'||!/^[a-z][a-z0-9_]{0,31}$/.test(n.id)||ids.has(n.id)||!kinds.includes(n.type as NodeKind))return fail();
    const p=savedRecord(n.parameters);if(['source','table','gallery','summary'].includes(String(n.type))&&Object.keys(p).length)return fail();
    if(n.type==='filter'){exact(p,['where']);if(p.where!==null)savedRecord(p.where);}
    if(n.type==='selection'){exact(p,['mode','ids']);if(!['include','exclude'].includes(String(p.mode))||!Array.isArray(p.ids)||!p.ids.length||p.ids.length>1000||p.ids.some(id=>typeof id!=='string'||!/^(?:[A-Za-z0-9_-]{11}|UC[A-Za-z0-9_-]{22})$/.test(id))||new Set(p.ids).size!==p.ids.length)return fail();}
    if(n.type==='calendar'){exact(p,'time_zone' in p?['year','time_zone']:['year']);if(!Number.isInteger(p.year)||Number(p.year)<1||Number(p.year)>9999||('time_zone' in p&&(typeof p.time_zone!=='string'||p.time_zone.length>64)))return fail();}
    if(n.type==='history'){exact(p,['metric','hours','bucket_hours']);if(!['views','subscribers'].includes(String(p.metric))||!Number.isInteger(p.hours)||Number(p.hours)<1||Number(p.hours)>696||![1,6,24].includes(Number(p.bucket_hours)))return fail();}
    ids.add(n.id);nodes.push(n as GraphNode);
  }
  const roots=nodes.filter(n=>n.type==='source'),outputs=nodes.filter(n=>sinks.includes(n.type));if(roots.length!==1||!outputs.length||outputs.length>4)return fail();
  const edges:Graph['edges']=[],parents=new Map<string,string>();
  for(const raw of g.edges){const e=exact(raw,['from','to','input','output']);if(typeof e.from!=='string'||typeof e.to!=='string'||!ids.has(e.from)||!ids.has(e.to)||parents.has(e.to)||e.input!=='query'||e.output!=='query'||sinks.includes(nodes.find(n=>n.id===e.from)!.type)||nodes.find(n=>n.id===e.to)!.type==='source')return fail();parents.set(e.to,e.from);edges.push(e as Graph['edges'][number]);}
  if(parents.size!==nodes.length-1)return fail();
  for(const id of ids){const seen=new Set<string>();let current=id;while(parents.has(current)){if(seen.has(current))return fail();seen.add(current);current=parents.get(current)!;}if(current!==roots[0].id)return fail();}
  const layout=savedRecord(g.layout);if(Object.keys(layout).sort().join()!==[...ids].sort().join())return fail();
  for(const p of Object.values(layout)){const o=exact(p,['x','y']);if(!Number.isInteger(o.x)||!Number.isInteger(o.y)||Math.abs(Number(o.x))>10000||Math.abs(Number(o.y))>10000)return fail();}
  return {version:1,nodes,edges,notes:g.notes,layout:layout as Graph['layout']};
}
export function parseResearchDefinition(value:unknown):ResearchDefinition{
  const root=exact(value,['version','query','graph']);if(root.version!==3||new TextEncoder().encode(JSON.stringify(value)).length>16384)return fail();
  const query=parseSavedDefinition({version:2,query:root.query}).query,graph=parseGraph(root.graph);
  // A block's query is not a Saved v2 object: its selection IDs (1..1000, checked by parseGraph) count toward
  // this 16 KiB definition like on the server, not toward the 8 KiB Saved query limit. Check its shape with one ID.
  const shape:Graph={...graph,nodes:graph.nodes.map(n=>n.type==='selection'?{...n,parameters:{...n.parameters,ids:(n.parameters.ids as string[]).slice(0,1)}}:n)};
  for(const node of shape.nodes)parseSavedDefinition({version:2,query:nodeQuery({version:1,query,graph:shape},node.id)});
  return {version:3,query,graph};
}
/** A saved canvas (Saved conditions version 4): only its graph, bound to no channel; results are never saved.
 *  The server (graph.normalize_saved_canvas) checks every rule of the graph; this reads the shape the Saved list,
 *  a backup and opening a canvas need. No canvas code is loaded here. */
export type SavedCanvas={version:4;graph:CanvasGraph};
const CANVAS_KINDS=['source','filter','sort','selection','gallery','table','summary','calendar','history'];
// The server's budgets of a saved canvas (graph.MAX_DEFINITION, graph.CANVAS_NOTES_BYTES), in UTF-8 bytes.
export const SAVED_CANVAS_BYTES=16384,CANVAS_NOTES_BYTES=8192;
// A canvas kept in this tab (history entry), unsaved edits included: restore drafts above the save limit
// so the user can shorten them before saving.
export const CANVAS_DRAFT_BYTES=65536;
export function parseSavedCanvas(value:unknown):SavedCanvas{
  const canvas=parseCanvasShape(value,SAVED_CANVAS_BYTES+32);
  if(new TextEncoder().encode(canvas.graph.notes).length>CANVAS_NOTES_BYTES)return fail();
  return canvas;
}
/** A canvas of this tab's history: the shape of a saved canvas, without its byte budgets. */
export function parseCanvasDraft(value:unknown):SavedCanvas{return parseCanvasShape(value,CANVAS_DRAFT_BYTES);}
function parseCanvasShape(value:unknown,maxBytes:number):SavedCanvas{
  const root=exact(value,['version','graph']),g=exact(root.graph,['version','nodes','edges','notes','layout']);
  if(root.version!==4||g.version!==2||typeof g.notes!=='string'||!Array.isArray(g.nodes)||!Array.isArray(g.edges)||!g.nodes.length||g.nodes.length>16||g.edges.length>16)return fail();
  if(new TextEncoder().encode(canonicalSaved(root)).length>maxBytes)return fail();
  const ids=new Set<string>();
  for(const raw of g.nodes){const n=exact(raw,['id','type','parameters']);if(typeof n.id!=='string'||!/^[a-z][a-z0-9_]{0,31}$/.test(n.id)||ids.has(n.id)||!CANVAS_KINDS.includes(String(n.type)))return fail();savedRecord(n.parameters);ids.add(n.id);}
  for(const raw of g.edges){const e=exact(raw,['from','to','output','input']);if(!ids.has(String(e.from))||!ids.has(String(e.to))||e.output!=='rows'||e.input!=='rows')return fail();}
  const layout=savedRecord(g.layout);if(Object.keys(layout).sort().join()!==[...ids].sort().join())return fail();
  for(const p of Object.values(layout)){const o=exact(p,['x','y']);if(!Number.isInteger(o.x)||!Number.isInteger(o.y)||Math.abs(Number(o.x))>10000||Math.abs(Number(o.y))>10000)return fail();}
  return structuredClone(root) as SavedCanvas;
}
export const MAX_RESEARCH_BYTES=16384;
const utf8=(value:unknown)=>new TextEncoder().encode(canonicalSaved(value)).length;
/** The larger count the server applies: the graph when it runs, the whole normalized v3 when it is saved. */
export function researchSize(document:ResearchDocument):{bytes:number;valid:boolean}{
  const raw=Math.max(utf8(document.graph),utf8({version:3,query:document.query,graph:document.graph}));
  if(raw>MAX_RESEARCH_BYTES)return {bytes:raw,valid:false};
  try{const bytes=Math.max(raw,utf8(researchDefinition(document).conditions));return {bytes,valid:bytes<=MAX_RESEARCH_BYTES};}
  catch{return {bytes:raw,valid:false};}
}
// A source not chosen yet is stored as an empty id; checks use a well-formed stand-in of the same kind.
const unchosen={channel:'$channel',folder:'00000000-0000-0000-0000-000000000000',label:'monetized',collection:'00000000-0000-4000-8000-000000000000'} as const;
/** A study document from this tab's history: the same rules as a saved study, except that its source may still be unchosen. */
export function parseStudyDocument(value:unknown):ResearchDocument{
  const root=exact(value,['version','query','graph']),query=savedRecord(root.query),scope=savedRecord(query.scope);
  if(root.version!==1||typeof scope.id!=='string')return fail();
  const kind=scope.kind as keyof typeof unchosen;if(!Object.hasOwn(unchosen,kind))return fail();
  if(kind==='channel'&&scope.id!==''&&!/^UC[A-Za-z0-9_-]{22}$/.test(scope.id))return fail();
  const bound={...query,scope:{...scope,id:kind==='channel'||scope.id===''?unchosen[kind]:scope.id}};
  const graph=parseResearchDefinition({version:3,query:bound,graph:root.graph}).graph;
  return {version:1,query:structuredClone(query) as SavedQuery,graph};
}
/** A Saved v2 query opens as a study without loss: the same query, source → one result block. */
export function sourceStudy(query:SavedQuery,result:'table'|'gallery'='table'):ResearchDocument{
  const nodes:GraphNode[]=[{id:'source',type:'source',parameters:{}},{id:'result',type:result,parameters:{}}];
  return {version:1,query:structuredClone(query),graph:{version:1,nodes,edges:[{from:'source',to:'result',input:'query',output:'query'}],notes:'',
    layout:{source:{x:24,y:36},result:{x:304,y:36}}}};
}
/** Binds a Saved v2 query's channel; an unknown or missing channel leaves the source to be chosen. */
export function studyFromSavedQuery(definition:SavedQueryDefinition,channelId:string|null,result:'table'|'gallery'='table'):ResearchDocument{
  // Only the query becomes the study; a presentation block (where Library shows it) does not.
  const query=structuredClone(parseSavedDefinition({version:2,query:definition.query}).query);
  if(query.scope.kind==='channel')query.scope.id=channelId&&/^UC[A-Za-z0-9_-]{22}$/.test(channelId)?channelId:'';
  return sourceStudy(query,result);
}
// Starting views and their columns. A row kind's default columns are its first view's.
const LATEST_COLUMNS:ResearchField[]=['video.title','channel.title','video.published_at','video.views'];
export const CHANNEL_COLUMNS:ResearchField[]=['channel.title','channel.subscribers','channel.views','channel.video_count','channel.country_code','channel.created_at'];
/** A new study: Latest uploads as a table, with the source still to be chosen. The catalogue's remembered
 *  columns and view (an old browser preference, read only) replace the first view's when present. */
export function newStudy(preference?:{view:'table'|'gallery';columns:ResearchField[]}):ResearchDocument{
  const columns=preference?.columns.filter(id=>id!=='channel.id'),gallery=preference?.view==='gallery';
  const query:SavedQuery={query_version:1,source:{id:videoSource},scope:{kind:'folder',id:'',include_descendants:false},video_types:['long'],
    select:columns?.length?columns:[...LATEST_COLUMNS],where:null,order_by:[{field:'video.published_at',direction:'desc',nulls:'last'}],
    coverage:{mode:'available'},consistency:'current',page:{size:25}};
  return sourceStudy(gallery?withGalleryImage(query):query,gallery?'gallery':'table');
}
/** A gallery shows each row's image, so the study's columns gain the image field of its rows
 *  (video thumbnail or channel image) when it is missing; the other columns and their order stay. */
export function withGalleryImage(query:SavedQuery):SavedQuery{
  const image:ResearchField=query.source.id===channelSource?'channel.avatar_url':'video.thumbnail_url';
  return query.select.includes(image)?query:{...query,select:[...query.select,image]};
}
export function researchDefinition(document:ResearchDocument):{channel_id:string|null;conditions:ResearchDefinition}{
  const query=structuredClone(document.query),channel_id=query.scope.kind==='channel'?query.scope.id:null;
  if(channel_id)query.scope.id='$channel';
  return {channel_id,conditions:parseResearchDefinition({version:3,query,graph:document.graph})};
}
/** Replace only the study's source; blocks, notes, layout and the Saved identity stay as they are. */
export function withScope(document:ResearchDocument,scope:{kind:'channel'|'folder'|'label'|'collection';id:string}):ResearchDocument{
  const query=structuredClone(document.query);
  query.scope=scope.kind==='folder'?{kind:'folder',id:scope.id,include_descendants:false}:{kind:scope.kind,id:scope.id};
  return {...document,query};
}
/** Change parts of the study's root query (format, conditions, sorting, columns, completeness, window, zone).
 *  A key set to undefined is removed. Blocks, notes and layout stay. Used without ResearchFilters on purpose:
 *  researchQuery() would add presentation fields and change page.size, altering a saved query. */
export function withSourceQuery(document:ResearchDocument,patch:Partial<SavedQuery>):ResearchDocument{
  const query={...structuredClone(document.query),...structuredClone(patch)} as Record<string,unknown>;
  for(const [key,value] of Object.entries(patch))if(value===undefined)delete query[key];
  return {...document,query:query as SavedQuery};
}
/** The study's root query for the other row kind: the same source (channels have no collections),
 *  zone and completeness rule; that kind's first view (Latest uploads or Channel list). */
export function entityQuery(query:SavedQuery,source:ResearchSource):SavedQuery{
  const base=structuredClone(query) as SavedQuery&Record<string,unknown>,channels=source===channelSource;
  if(base.source.id===source)return base;
  delete base.observations;delete base.video_types;
  base.source={id:source};base.where=null;
  if(channels){if(base.scope.kind==='collection')base.scope={kind:'folder',id:'',include_descendants:false};
    base.select=[...CHANNEL_COLUMNS];base.order_by=[{field:'channel.subscribers',direction:'desc',nulls:'last'}];}
  else{base.video_types=['long'];base.select=[...LATEST_COLUMNS];base.order_by=[{field:'video.published_at',direction:'desc',nulls:'last'}];}
  if(base.coverage)base.coverage={mode:base.coverage.mode};
  return base;
}
export function openResearch(definition:ResearchDefinition,channelId:string|null):ResearchDocument{
  const clean=parseResearchDefinition(definition),query=structuredClone(clean.query);
  if(query.scope.kind==='channel'){if(!channelId||!/^UC[A-Za-z0-9_-]{22}$/.test(channelId))return fail();query.scope.id=channelId;}
  return {version:1,query,graph:clean.graph};
}
/** The chain of blocks from the study source to this block, source first. */
export function nodePath(graph:Graph,id:string):GraphNode[]{
  const nodes=new Map(graph.nodes.map(n=>[n.id,n])),path:GraphNode[]=[];let node=nodes.get(id);
  while(node){path.unshift(node);const edge=graph.edges.find(e=>e.to===node!.id);node=edge?nodes.get(edge.from):undefined;}
  return path;
}
/** Builds the expected query definition only; execution and permissions are server-owned. */
export function nodeQuery(document:ResearchDocument,id:string):SavedQuery{
  const graph=parseGraph(document.graph);if(!graph.nodes.some(n=>n.id===id))return fail();const path=nodePath(graph,id);
  const query=structuredClone(document.query);
  for(const n of path){let leaf:Expression|null=null;if(n.type==='filter')leaf=n.parameters.where as Expression|null;
    if(n.type==='selection'){leaf={field:query.source.id===channelSource?'channel.id':'video.id',op:'in',value:n.parameters.ids as string[]};if(n.parameters.mode==='exclude')leaf={not:leaf};}
    if(leaf)query.where=query.where?{all:[query.where,leaf]}:leaf;
  }return query;
}
/** Every field the study depends on: root columns, sorting and conditions, and every Conditions block. */
export function studyFields(document:ResearchDocument):string[]{
  const q=document.query;
  return [...new Set<string>([...q.select,...q.order_by.map(s=>s.field),...expressionFields(q.where??null),
    ...document.graph.nodes.flatMap(n=>n.type==='filter'?expressionFields((n.parameters.where??null) as Expression|null):[])])];
}
const TOKEN=/^[\x21-\x7e]{1,16384}$/;
/** A table or gallery page: a current page, or a page of one fixed (frozen) list with its position. */
export function parseBlockPage(value:unknown,filters:ResearchFilters):BlockPage{
  const root=savedRecord(value),context=savedRecord(root.context);
  if(context.consistency!=='frozen')return parseResearchPage(value,filters);
  const page=exact(root.page,['has_more','next_cursor','previous_cursor','start_index','total_count','anchor_id']),rows=Array.isArray(root.rows)?root.rows.length:-1;
  const start=Number(page.start_index),total=Number(page.total_count),cursor=(item:unknown)=>item===null||typeof item==='string'&&TOKEN.test(item);
  if(typeof context.frozen_ref!=='string'||!TOKEN.test(context.frozen_ref)||typeof context.time_zone!=='string'||context.time_zone.length>64
    ||!Number.isSafeInteger(page.start_index)||start<0||!Number.isSafeInteger(page.total_count)||total>50000||rows<0||start+rows>total
    ||!cursor(page.next_cursor)||!cursor(page.previous_cursor)||(start>0)!==(page.previous_cursor!==null)
    ||!page.has_more&&start+rows!==total||page.anchor_id!==null&&(typeof page.anchor_id!=='string'||page.anchor_id.length>64))return fail();
  // Rows, cells, provenance, coverage and clocks pass the same checks as a current page.
  parseResearchPage({...root,context:{...context,consistency:'current'},page:{has_more:page.has_more,next_cursor:page.next_cursor}},filters);
  return value as ResearchFrozenPage;
}
/** The filters a block's page is checked against: its own query (source, scope, columns, page size). */
export function blockFilters(document:ResearchDocument,id:string):ResearchFilters{
  const definition=researchDefinition({...document,query:nodeQuery(document,id)});
  return savedQueryFilters({version:2,query:definition.conditions.query},definition.channel_id);
}
function stampOf(value:unknown):number{return typeof value==='string'&&/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/.test(value)?Date.parse(value):NaN;}
/** A block's paging query from the server: the query the block ran, without a continuation. */
function pagingQuery(value:unknown,document:ResearchDocument,consistency:'current'|'frozen'):Record<string,unknown>{
  const q=savedRecord(value),page=exact(q.page,['size']);
  if(savedRecord(q.source).id!==document.query.source.id||q.consistency!==consistency||!Number.isInteger(page.size)||Number(page.size)<1||Number(page.size)>200
    ||new TextEncoder().encode(JSON.stringify(q)).length>65536)return fail();
  return q;
}
export async function runResearch(document:ResearchDocument,signal:AbortSignal,run?:RunOptions):Promise<GraphResult>{
  const value=await requestJson('/research/graphs/execute',signal,{method:'POST',headers:{'Content-Type':'application/json'},body:canonicalSaved(run?{...document,run}:document)});
  const root=savedRecord(value),context=savedRecord(root.context),results=savedRecord(root.nodes);
  let fixed:FixedListRun|null=null;
  if(run){const r=exact(root.run,['consistency','fixed']);if(r.consistency!==run.consistency)return fail();
    if(r.fixed!==null){const f=exact(r.fixed,['reused','evaluated_at','valid_before','total_count']);
      if(run.consistency!=='frozen'||typeof f.reused!=='boolean'||f.reused&&!('frozen_ref' in run&&run.frozen_ref)||!(stampOf(f.valid_before)>stampOf(f.evaluated_at))||!Number.isSafeInteger(f.total_count)||Number(f.total_count)<0)return fail();
      fixed=f as FixedListRun;}
  }else if(root.run!==undefined)return fail();
  if(root.version!==1||!['ready','partial'].includes(String(root.status))||typeof root.graph_fingerprint!=='string'||!/^[a-f0-9]{64}$/.test(root.graph_fingerprint)
    ||typeof context.evaluated_at!=='string'||!Number.isFinite(Date.parse(context.evaluated_at))||typeof context.valid_before!=='string'||!Number.isFinite(Date.parse(context.valid_before))||Date.parse(context.valid_before)<=Date.parse(context.evaluated_at)
    ||context.consistency!==(fixed?'fixed_source_list':'sequential_current_reads')||!sourceLifetime(value)||sourceExpired(value)||root.source_expires_at!==context.valid_before
    ||Object.keys(results).sort().join()!==document.graph.nodes.map(n=>n.id).sort().join())return fail();
  for(const node of document.graph.nodes){const r=savedRecord(results[node.id]),sink=sinks.includes(node.type);
    // With run options every result block says how it was read; only a row block fed by the source can be fixed.
    if(sink&&run&&(r.consistency!=='current'&&r.consistency!=='frozen'||r.consistency==='frozen'&&(run.consistency!=='frozen'||!ROW_KINDS.includes(node.type))))return fail();
    if(!(sink&&run)&&r.consistency!==undefined)return fail();
    if(r.state==='error'){if(typeof r.code!=='string'||!/^[a-z_]{1,80}$/.test(r.code)||r.value!==undefined||r.path!==undefined&&r.path!==null&&(typeof r.path!=='string'||r.path.length>200))return fail();continue;}
    if(r.state!=='ready')return fail();if(!sink){if(r.output!=='query'||r.value!==undefined)return fail();continue;}
    const payload=savedRecord(r.value),nodeContext=savedRecord(payload.context);
    if(typeof nodeContext.valid_before!=='string'||!Number.isFinite(Date.parse(nodeContext.valid_before))||Date.parse(nodeContext.valid_before)<Date.parse(String(context.valid_before)))return fail();
    const tagged=transferSourceLifetime(value,{...payload,source_read_at:root.source_read_at,source_expires_at:nodeContext.valid_before});
    const q=nodeQuery(document,node.id);
    if(node.type in OPERATION_OF_BLOCK){
      if(r.output!=='operation'||r.query!==undefined)return fail();r.value=parseOperation(tagged,OPERATION_OF_BLOCK[node.type as keyof typeof OPERATION_OF_BLOCK],q.source.id);
    }else{
      if(r.output!=='page')return fail();
      const frozen=r.consistency==='frozen';
      if(run)r.query=pagingQuery(r.query,document,frozen?'frozen':'current');else if(r.query!==undefined)return fail();
      if((nodeContext.consistency==='frozen')!==frozen||frozen&&!fixed)return fail();
      r.value=parseBlockPage(tagged,blockFilters(document,node.id));
    }
  }return value as GraphResult;
}
// ---- Templates: each starts a new study from the current source and format. ----
export type StudyTemplate='latest'|'most_viewed'|'thumbnails'|'overview'|'observations'|'evidence'|'channel_list'|'compare_channels'|'channel_history';
export const TEMPLATE_NAMES:Record<StudyTemplate,string>={latest:'Latest uploads',most_viewed:'Most viewed',thumbnails:'Thumbnail gallery',overview:'Overview of this selection',
  observations:'Observations near age 24 hours',evidence:'Evidence and notes',channel_list:'Channel list',compare_channels:'Compare channels',channel_history:'Channel history'};
export const VIDEO_TEMPLATES:readonly StudyTemplate[]=['latest','most_viewed','thumbnails','overview','observations','evidence'];
export const CHANNEL_TEMPLATES:readonly StudyTemplate[]=['channel_list','compare_channels','channel_history','evidence'];
/** The canvas lists every template once: Videos and Channels start rows of their kind; a template on both
 *  lists keeps the Source's rows, so it has its own group. */
export const CANVAS_TEMPLATE_GROUPS:readonly {label:string;templates:readonly StudyTemplate[]}[]=[
  {label:'Videos',templates:VIDEO_TEMPLATES.filter(t=>!CHANNEL_TEMPLATES.includes(t))},
  {label:'Channels',templates:CHANNEL_TEMPLATES.filter(t=>!VIDEO_TEMPLATES.includes(t))},
  {label:'Videos or channels',templates:VIDEO_TEMPLATES.filter(t=>CHANNEL_TEMPLATES.includes(t))}];
/** The operations a template's blocks need; a template is offered only when the server grants at least one. */
export function templateOperations(template:StudyTemplate):string[]{
  return template==='overview'?[OPERATION_OF_BLOCK.summary,OPERATION_OF_BLOCK.calendar]:template==='channel_history'?[OPERATION_OF_BLOCK.history]:[];
}
/** A template applied to the study's source (scope, format, zone). Starting views set columns and sorting and clear
 *  the conditions; the overview, observations and evidence keep the whole query. */
export function researchTemplate(query:SavedQuery,template:StudyTemplate,operations?:readonly string[]):ResearchDocument{
  const channelTemplate=['channel_list','compare_channels','channel_history'].includes(template);
  let q=structuredClone(query);
  if(channelTemplate&&q.source.id!==channelSource)q=entityQuery(q,channelSource);
  if(['latest','most_viewed','thumbnails','overview','observations'].includes(template)&&q.source.id!==videoSource)q=entityQuery(q,videoSource);
  const view=(select:ResearchField[],field:ResearchField)=>{q.select=select;q.order_by=[{field,direction:'desc',nulls:'last'}];q.where=null;};
  if(template==='latest')view([...LATEST_COLUMNS],'video.published_at');
  if(template==='most_viewed')view(['video.title','channel.title','video.views','video.likes','video.comments'],'video.views');
  if(template==='thumbnails')view(['video.title','video.thumbnail_url','channel.title','video.views','video.published_at'],'video.views');
  if(template==='channel_list'||template==='compare_channels')view([...CHANNEL_COLUMNS],'channel.subscribers');
  if(template==='observations')q.select=Array.from(new Set([...q.select,'observation.views_at_age' as ResearchField]));
  if(template==='evidence')q.select=Array.from(new Set([...q.select,...(q.source.id===channelSource?['channel.title','channel.avatar_url'] as const:['video.title','video.thumbnail_url'] as const)]));
  const source:GraphNode={id:'source',type:'source',parameters:{}};
  const allowed=(type:keyof typeof OPERATION_OF_BLOCK)=>!operations||operations.includes(OPERATION_OF_BLOCK[type]);
  if(template==='overview'||template==='channel_history'){
    const outputs:GraphNode[]=template==='overview'
      ?[...(allowed('summary')?[{id:'fingerprint',type:'summary' as const,parameters:{}}]:[]),...(allowed('calendar')?[{id:'calendar',type:'calendar' as const,parameters:{year:new Date().getUTCFullYear()}}]:[])]
      :[{id:'history',type:'history',parameters:{metric:'views',hours:696,bucket_hours:24}}];
    if(!outputs.length)return fail();
    const nodes=[source,...outputs];
    return {version:1,query:q,graph:{version:1,nodes,edges:outputs.map(n=>({from:'source',to:n.id,input:'query',output:'query'})),notes:'',
      layout:Object.fromEntries(nodes.map((n,i)=>[n.id,i?{x:304,y:36+(i-1)*160}:{x:24,y:36}]))}};
  }
  let nodes:GraphNode[]=[source,{id:'result',type:template==='thumbnails'?'gallery':'table',parameters:{}}];
  if(template==='evidence')nodes=[source,{id:'conditions',type:'filter',parameters:{where:null}},{id:'result',type:'gallery',parameters:{}}];
  const graph:Graph={version:1,nodes,edges:nodes.slice(1).map((n,i)=>({from:nodes[i].id,to:n.id,input:'query',output:'query'})),
    notes:template==='evidence'?'Research question:\n\nMy observations and links to evidence:\n':'',layout:Object.fromEntries(nodes.map((n,i)=>[n.id,{x:24+i*280,y:36}]))};
  return {version:1,query:q,graph};
}
/** Switching the study between video and channel rows starts that kind's first view; name and notes stay. */
export function studyForRows(document:ResearchDocument,source:ResearchSource):ResearchDocument{
  const next=researchTemplate(entityQuery(document.query,source),source===channelSource?'channel_list':'latest');
  return {...next,graph:{...next.graph,notes:document.graph.notes}};
}
/** What stops a study from running or saving, in words; [] when nothing does. */
export function studyProblems(document:ResearchDocument):string[]{
  const problems:string[]=[];
  // History needs one channel; without it the server refuses the whole study.
  if(document.graph.nodes.some(n=>n.type==='history')&&document.query.scope.kind!=='channel')
    problems.push('Channel history needs one channel. Choose a channel or remove the history block.');
  const size=researchSize(document);
  if(size.bytes>MAX_RESEARCH_BYTES)problems.push(`The study is too large to save: ${size.bytes.toLocaleString('en-US')} of ${MAX_RESEARCH_BYTES.toLocaleString('en-US')} bytes. Shorten notes or the ID list.`);
  return problems;
}
