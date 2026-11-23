import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
import * as lifetime from '../application/frontend/src/sourceLifetime.ts';
import * as savedLibrary from '../application/frontend/src/explorer/libraryData/savedLibrary.ts';
import * as React from '../application/frontend/node_modules/react/index.js';
import * as jsxRuntime from '../application/frontend/node_modules/react/jsx-runtime.js';
import { renderToStaticMarkup } from '../application/frontend/node_modules/react-dom/server.node.js';
function compiled(file, imports={}) {
  const {outputText}=ts.transpileModule(readFileSync(new URL('../application/frontend/src/explorer/'+file,import.meta.url),'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}});
  const exports={};new Function('require','exports',outputText)(name=>{assert.ok(name in imports,name);return imports[name];},exports);return exports;
}
const saved=compiled('savedWork.ts');
const research=compiled('researchQuery.ts',{'./api':{ExplorerApiError:class extends Error{}},'../sourceLifetime':lifetime});
const definition=compiled('savedQueryDefinition.ts',{'./researchQuery':research,'./savedWork':saved,'./timeZones':compiled('timeZones.ts')});
const operations=compiled('researchOperations.ts',{'./api':{},'../sourceLifetime':lifetime});
const graph=compiled('researchGraph.ts',{'./api':{},'./savedWork':saved,'./savedQueryDefinition':definition,'./researchQuery':research,'./researchOperations':operations,'../sourceLifetime':lifetime});
const workshopApi={ExplorerApiError:class extends Error{constructor(message,status,code){super(message);this.status=status;this.code=code;}},requestJson(){throw new Error('no request while rendering');}};
const views=compiled('savedViews.ts',{'./researchGraph':graph,'./api':{},'./savedQueryDefinition':definition,'../explorerRoutes':{EXPLORER_V3_ROOT:'/app-explorer-v3'},'./catalogueQuery':{},'./savedWork':saved,'./timeZones':compiled('timeZones.ts'),'./libraryData/savedLibrary.ts':savedLibrary});
const backup=compiled('userWorkBackup.ts',{'./researchFieldPreferences':compiled('researchFieldPreferences.ts'),'./savedViews':views,'./savedWork.ts':saved,'./catalogueQuery.ts':{},'./cataloguePreferences.ts':{catalogueDensityPreference:{isValid:x=>['large','compact'].includes(x)}},'../state/browserPreferences.ts':{},'./bookmarks.ts':{},'./folderCollapse.ts':{}});
const id='11111111-1111-4111-8111-111111111111';
const filters={scope:{kind:'folder',id},type:'stream',columns:['video.views','observation.views_per_hour'],where:{field:'video.views',op:'gte',value:'9007199254740993'},order:[{field:'video.views',direction:'desc'}],complete:false,
 observations:{version:1,window_seconds:86400,tolerance_seconds:7200,age_seconds:86400,age_tolerance_seconds:7200}};
const value=definition.parseSavedDefinition({version:2,query:research.researchQuery(filters)});
assert.deepEqual(definition.parseSavedDefinition(value),value);
assert.equal(definition.savedQueryFilters(value,null).observations.window_seconds,86400);
assert.equal(definition.savedQueryFilters(value,null).where.value,'9007199254740993');
for(const mutate of [x=>x.query.rows=[],x=>x.query.page.after='cursor',x=>x.api_key='secret',x=>x.query.observations.tolerance_seconds=99999,x=>x.query.observations.window_seconds='3600',x=>x.query.scope.id='all']) {
 const bad=structuredClone(value);mutate(bad);assert.throws(()=>definition.parseSavedDefinition(bad));
}
const narrow=structuredClone(value);narrow.query.select=['video.views'];
assert.deepEqual(research.researchQuery(definition.savedQueryFilters(narrow,null)).select,['video.views']);
const channel=structuredClone(value);channel.query.scope={kind:'channel',id:'$channel'};
assert.equal(definition.savedQueryFilters(channel,'UC'+'a'.repeat(22)).scope.id,'UC'+'a'.repeat(22));
assert.throws(()=>definition.savedQueryFilters(channel,null));
const row={id,kind:'view',name:'My study',channel_id:null,conditions:value,revision:1,created_at:'2026-10-01T00:00:00Z',updated_at:'2026-10-01T00:00:00Z',channel_available:true};
const parsed=views.parseSavedView(row);
assert.equal(views.savedViewUrl(parsed,'http://localhost:8002').searchParams.get('saved'),id);
assert.deepEqual(views.parseSavedViewsPage({contract_version:1,views:[row],next_cursor:null}).views[0],parsed);
const {kind,channel_id,channel_available,...portable}=row;
const file={format:'leti-cook-user-work',version:5,created_at:row.created_at,user_work:{folders:[],labels:[],channels:[],collections:[],reviews:[],queries:[portable],field_preferences:{version:1,defaults_profile:'public-current-v1',lifecycle_profile:'public-696h-v1',workspace:{},sources:{},description_setup:'pending'}},browser_preferences:{catalogue_density:'large',bookmarked_channel_refs:[],collapsed_folder_ids:[]}};
assert.equal(backup.parseUserWorkBackup(file).user_work.queries[0].conditions.query.video_types[0],'stream');
const preview=backup.parseUserWorkPreview({counts:{folders:0,labels:0,channels:0,collections:0,reviews:0,queries:1},can_restore:true,backup:file});
assert.equal(preview.counts.queries,1);
for(const key of ['rows','last_result','access_token']){const bad=structuredClone(file);bad.user_work.queries[0][key]=[];assert.throws(()=>backup.parseUserWorkBackup(bad));}
console.log('Saved v2 query, source bindings, exact numbers, observation windows and backup v5 passed.');

// A Library save is a v2 query with a closed presentation block; old v2 objects stay valid.
const library={version:1,surface:'library',view:'grid',density:'compact',columns:['video.views'],time_zone:'Europe/Berlin'};
const librarySave=structuredClone(channel);librarySave.presentation=library;
assert.deepEqual(definition.parseSavedDefinition(librarySave).presentation,library);
assert.equal(definition.savedQueryFilters(librarySave,'UC'+'a'.repeat(22)).scope.id,'UC'+'a'.repeat(22));
assert.equal(Object.hasOwn(definition.parseSavedDefinition(channel),'presentation'),false);
assert.equal(views.parseSavedView({...row,channel_id:'UC'+'a'.repeat(22),conditions:librarySave}).conditions.presentation.time_zone,'Europe/Berlin');
for(const patch of [{extra:1},{version:2},{surface:'workshop'},{view:'list'},{density:'cosy'},{columns:[]},{columns:['video.likes']},
 {columns:['video.views','video.views']},{time_zone:'local'},{time_zone:'Europe/'+'x'.repeat(60)}]) {
 const bad=structuredClone(librarySave);Object.assign(bad.presentation,patch);assert.throws(()=>definition.parseSavedDefinition(bad));
}
for(const drop of ['version','surface','view']){const bad=structuredClone(librarySave);delete bad.presentation[drop];assert.throws(()=>definition.parseSavedDefinition(bad));}
assert.throws(()=>definition.parseSavedDefinition({...value,presentation:library}));
const libraryFile=structuredClone(file);libraryFile.user_work.queries=[];
libraryFile.user_work.channels=[{id:'55555555-5555-4555-8555-555555555555',user_input:null,folder_id:null,parent_ref:null,is_hidden:false,added_at:null,label_keys:[],
 saved_views:[{id:'66666666-6666-4666-8666-666666666666',name:'Library save',conditions:librarySave,revision:1,created_at:row.created_at,updated_at:row.updated_at}]}];
assert.deepEqual(backup.parseUserWorkBackup(libraryFile).user_work.channels[0].saved_views[0].conditions.presentation,library);
console.log('Library presentation block: strict, optional, channel-bound, kept by the backup codec.');

// The server accepts any IANA zone in query.time; one such object must not make
// the whole Saved list (or a backup) unreadable. Offsets and free text stay refused.
{
  const zoned=structuredClone(value);zoned.query.time={mode:'current',timezone:'Europe/Moscow'};
  assert.equal(definition.parseSavedDefinition(zoned).query.time.timezone,'Europe/Moscow');
  for(const zone of ['UTC','America/Argentina/Buenos_Aires','Etc/GMT+3','Asia/Tokyo'])
    assert.equal(definition.parseSavedDefinition({...zoned,query:{...zoned.query,time:{mode:'current',timezone:zone}}}).query.time.timezone,zone);
  for(const time of [{mode:'current',timezone:'+03:00'},{mode:'current',timezone:'local'},{mode:'current',timezone:'Europe/../x'},
    {mode:'current',timezone:'Europe/'+'x'.repeat(60)},{mode:'current',timezone:5},{mode:'current',timezone:''},{mode:'current'},
    {mode:'fixed',timezone:'Europe/Moscow'},{mode:'current',timezone:'Europe/Moscow',offset:'+03:00'}])
    assert.throws(()=>definition.parseSavedDefinition({...zoned,query:{...zoned.query,time}}),JSON.stringify(time));
  const moscowRow={...row,id:'22222222-2222-4222-8222-222222222222',name:'Moscow dates',conditions:zoned};
  const study=graph.researchDefinition({version:1,query:zoned.query,graph:graph.researchTemplate(zoned.query,'evidence').graph}).conditions;
  const studyRow={...row,id:'33333333-3333-4333-8333-333333333333',name:'Moscow study',conditions:study};
  const page=views.parseSavedViewsPage({contract_version:1,views:[row,moscowRow,studyRow],next_cursor:null});
  assert.deepEqual(page.views.map(view=>view.name),['My study','Moscow dates','Moscow study']);
  assert.equal(page.views[2].conditions.query.time.timezone,'Europe/Moscow');
  const zonedFile=structuredClone(file);zonedFile.user_work.queries[0].conditions=zoned;
  assert.equal(backup.parseUserWorkBackup(zonedFile).user_work.queries[0].conditions.query.time.timezone,'Europe/Moscow');
  const broken={...row,id:'44444444-4444-4444-8444-444444444444',conditions:{...zoned,query:{...zoned.query,time:{mode:'current',timezone:'+03:00'}}}};
  // The broken item is listed as unreadable (no conditions, no buttons); the readable one stays as it is.
  assert.throws(()=>views.parseSavedView(broken));
  const listed=views.parseSavedViewsPage({contract_version:1,views:[row,broken],next_cursor:null}).views;
  assert.deepEqual(listed[0],parsed);
  assert.deepEqual(listed[1],{kind:'unreadable',id:broken.id,name:broken.name});
}
console.log('Saved list with a non-UTC v2 query and v3 study: readable; strict zone grammar kept.');

// The server's text search operator carries a version digit (contains_casefold_v1, registry.py). A saved
// query or study with a "contains" condition is readable and can be saved; malformed operators stay refused.
{
  const search=structuredClone(value);search.query.where={all:[search.query.where,{field:'video.title',op:'contains_casefold_v1',value:'soup'}]};
  assert.equal(definition.parseSavedDefinition(search).query.where.all[1].op,'contains_casefold_v1');
  const searchRow={...row,id:'55555555-5555-4555-8555-555555555555',name:'Soup',conditions:search};
  assert.equal(views.parseSavedViewsPage({contract_version:1,views:[row,searchRow],next_cursor:null}).views.length,2);
  const searchStudy=graph.researchTemplate(search.query,'evidence');searchStudy.graph.nodes[1].parameters.where={field:'video.description',op:'contains_casefold_v1',value:'recipe'};
  assert.equal(graph.researchDefinition(searchStudy).conditions.graph.nodes[1].parameters.where.op,'contains_casefold_v1');
  for(const op of ['1eq','Contains','contains-casefold','x'.repeat(33),'_eq',''])
    assert.throws(()=>definition.parseSavedDefinition({...search,query:{...search.query,where:{field:'video.title',op,value:'soup'}}}),op);
}
console.log('Saved text-search conditions (contains_casefold_v1) are readable and savable.');

// Channel grain keeps the common Saved format without video-only options.
const cq=research.researchQuery({...filters,source:research.channelSource,type:'long',observations:undefined,
 columns:['channel.id','channel.title','channel.subscribers'],order:[{field:'channel.subscribers',direction:'desc'}],where:null,exactProjection:true});
assert.equal(Object.hasOwn(cq,'video_types'),false);
const cd=definition.parseSavedDefinition({version:2,query:cq});
assert.deepEqual(research.researchQuery(definition.savedQueryFilters(cd,null)),research.researchQuery({...definition.savedQueryFilters(cd,null)}));
assert.equal(definition.savedQueryFilters(cd,null).source,research.channelSource);
for(const patch of [{video_types:['long']},{video_types:null},{observations:{}}])assert.throws(()=>definition.parseSavedDefinition({version:2,query:{...cq,...patch}}));

// The same graph underlies template, canvas, Saved and portable backup.
const document=graph.researchTemplate(value.query,'evidence');
const researchDefinition=graph.researchDefinition(document).conditions;
assert.equal(researchDefinition.version,3);
assert.deepEqual(graph.openResearch(researchDefinition,null),document);
const file5=structuredClone(file);file5.version=5;file5.user_work.field_preferences=compiled('researchFieldPreferences.ts').parseFieldPreferences({version:1,defaults_profile:'public-current-v1',lifecycle_profile:'public-696h-v1',workspace:{},sources:{},description_setup:'pending'});
file5.user_work.queries[0].conditions=researchDefinition;
assert.equal(backup.parseUserWorkBackup(file5).user_work.queries[0].conditions.graph.notes,document.graph.notes);
for(const mutate of [d=>d.graph.rows=[],d=>d.graph.nodes[1].parameters.sql='SELECT 1',d=>d.graph.edges[0].from='result',d=>d.graph.edges[0].output='rows',d=>d.graph.layout.source.x=NaN]){
 const bad=structuredClone(researchDefinition);mutate(bad);assert.throws(()=>graph.parseResearchDefinition(bad));
}
const badCondition=structuredClone(researchDefinition);badCondition.graph.nodes[1].parameters.where={results:[{title:'stored API data'}]};assert.throws(()=>graph.parseResearchDefinition(badCondition));
// A query or research with a deleted source still opens to change its source;
// only a channel-scoped v1 view needs its channel to open.
const list=readFileSync(new URL('../application/frontend/src/explorer/SavedViewsList.tsx',import.meta.url),'utf8');
assert.match(list,/disabled=\{Boolean\(busy\) \|\| \(!view\.channel_available && view\.conditions\.version === 1\)\}/);
assert.match(list,/const target = updated\.channel_available \|\| updated\.conditions\.version !== 1 \? savedLibraryTarget\(updated, window\.location\.origin\) : null;/);
assert.match(list,/else if \(target\.kind === 'workshop'\) onOpen\(updated\);/);
// The workshop then names the missing source in its Source block (sourceMissing): tests/test_workshop_handoff.mjs.
const workshop=readFileSync(new URL('../application/frontend/src/explorer/ResearchWorkshop.tsx',import.meta.url),'utf8');
// Calculation blocks are offered only when the server grants their operation (block registry).
{
  const handoffModule=compiled('workshopHandoff.ts',{'./savedQueryDefinition':definition,'./researchQuery':research,'./researchGraph':graph,'./savedViews':views,'./savedWork':saved});
  const model=compiled('workshopModel.ts',{'./api':workshopApi,'./researchGraph':graph,'./researchQuery':research,'./workshopHandoff':handoffModule});
  assert.deepEqual(model.offeredBlocks('video',[]),['filter','selection','table','gallery']);
  assert.deepEqual(model.offeredBlocks('video',[operations.summaryOperation]),['filter','selection','table','gallery','summary']);
  assert.deepEqual(model.offeredBlocks('channel',[operations.summaryOperation,operations.historyOperation]),['filter','selection','table','gallery','history']);
}
// Change windows: the window is selectable and the schedule's age limit is stated, not hidden.
const queryModule=readFileSync(new URL('../application/frontend/src/explorer/researchQuery.ts',import.meta.url),'utf8');
assert.match(queryModule,/observation_age_limits:limits/);
const ageNote=compiled('ObservationAgeLimitNote.tsx',{'react/jsx-runtime':jsxRuntime});
const rowsModule=compiled('ResearchRows.tsx',{'react/jsx-runtime':jsxRuntime,'./researchQuery':research,'./videoGrowth':compiled('videoGrowth.ts')});
const controls=compiled('ResearchControls.tsx',{'react/jsx-runtime':jsxRuntime,'../sourceLifetime':lifetime,'./ObservationAgeLimitNote':ageNote,'./ResearchRows':rowsModule,
  './researchQuery':research,'./researchOperations':operations});
const windowHtml=limits=>renderToStaticMarkup(React.createElement(controls.ObservationWindowPicker,{window:3600,limits,onChange(){}}));
assert.match(windowHtml({'3600':0,'86400':null}),/<option value="3600" disabled="" selected="">1 hour<\/option><option value="86400">1 day<\/option>/);
assert.match(windowHtml({'3600':0}),/No video is refreshed at least once per hour under the current collection schedule/);
assert.match(windowHtml({'3600':604800}),/Only videos up to 7 days old are shown: only they are refreshed at least once per hour/);
assert.doesNotMatch(windowHtml({'3600':null}),/Only videos up to|No video is refreshed/);
// The server's capabilities document for an edited schedule (0 / positive / null limits),
// pinned against the server by tests/test_observation_limits.py, passes the real client parser.
const serverDocument=JSON.parse(readFileSync(new URL('./fixtures/research_capabilities_window_limits.json',import.meta.url),'utf8'));
let served=serverDocument;
const live=compiled('researchQuery.ts',{'./api':{ExplorerApiError:class extends Error{},requestJson:async()=>served},'../sourceLifetime':lifetime});
const liveCapabilities=await live.loadResearchCapabilities(new AbortController().signal);
assert.deepEqual(liveCapabilities.observation_age_limits,{long:{'3600':0,'86400':31536000},short:{'3600':604800,'86400':31536000},stream:{'3600':null,'86400':null}});
assert.ok(liveCapabilities.fields.some(field=>field.id==="video.id"));
served=structuredClone(serverDocument);served.observation_age_limits.long['3600']=-1;
await assert.rejects(()=>live.loadResearchCapabilities(new AbortController().signal));
assert.match(windowHtml(liveCapabilities.observation_age_limits.long),/<option value="3600" disabled="" selected="">1 hour<\/option>/);
// A change field used only in a nested condition still counts as referenced.
const nested={all:[{field:'video.views',op:'gte',value:'1'},{not:{any:[{field:'video.title',op:'contains_casefold_v1',value:'x'},{field:'observation.views_delta',op:'gte',value:'1'}]}}]};
assert.deepEqual(research.expressionFields(nested),['video.views','video.title','observation.views_delta']);
const nestedStudy=graph.researchTemplate(value.query,'evidence');nestedStudy.graph.nodes[1].parameters.where=nested;
assert.ok(graph.studyFields(nestedStudy).includes('observation.views_delta'));
const blocksSource=readFileSync(new URL('../application/frontend/src/explorer/WorkshopBlocks.tsx',import.meta.url),'utf8');
assert.match(blocksSource,/const changeShown = studyFields\(document\)\.some\(field => changeFields\.includes\(field\)\)/);
assert.match(blocksSource,/<ObservationWindowPicker window=\{window\} limits=\{caps\?\.source_id === query\.source\.id \? caps\.observation_age_limits\[type\] : undefined\}/);
assert.match(blocksSource,/<ObservationAgeLimitNote limit=\{page\.context\.observation_age_limit_seconds\} \/>/);
// A study whose folder was deleted is
// opened, given another source and saved; reopening keeps blocks, notes and layout.
{
  const study=graph.researchTemplate(value.query,'evidence');study.graph.notes='Question and links';study.graph.layout.result={x:777,y:55};
  const savedStudy=graph.researchDefinition(study);
  const reopened=graph.openResearch(savedStudy.conditions,savedStudy.channel_id);
  const moved=graph.withScope(reopened,{kind:'folder',id:'22222222-2222-4222-8222-222222222222'});
  assert.deepEqual(moved.graph,study.graph);
  assert.deepEqual(moved.query.scope,{kind:'folder',id:'22222222-2222-4222-8222-222222222222',include_descendants:false});
  const resaved=graph.researchDefinition(moved);assert.equal(resaved.channel_id,null);
  assert.deepEqual(graph.openResearch(resaved.conditions,null).graph,study.graph);
  const toChannel=graph.researchDefinition(graph.withScope(moved,{kind:'channel',id:'UC'+'b'.repeat(22)}));
  assert.equal(toChannel.channel_id,'UC'+'b'.repeat(22));assert.equal(toChannel.conditions.query.scope.id,'$channel');
}
// A selection block takes up to 1000 IDs, as on the server: they count toward the study's 16 KiB, not toward
// the 8 KiB limit of a Saved v2 query (before: a study with more than 531 selected video IDs was refused).
{
  const pick=n=>Array.from({length:n},(_,i)=>i.toString(36).padStart(11,'_'));
  const study=graph.researchTemplate(value.query,'evidence');
  const selected=n=>({...study,graph:{...study.graph,nodes:study.graph.nodes.map(node=>node.type==='filter'?{id:node.id,type:'selection',parameters:{mode:'include',ids:pick(n)}}:node)}});
  for(const n of [532,1000])assert.equal(graph.researchDefinition(selected(n)).conditions.graph.nodes[1].parameters.ids.length,n);
  assert.throws(()=>graph.researchDefinition(selected(1001)));
  const shape=selected(1000);shape.graph.nodes[1].parameters.ids[999]='not an id';assert.throws(()=>graph.researchDefinition(shape));
}
assert.match(blocksSource,/onChange\(withScope\(document, \{ kind: scope\.kind, id: e\.target\.value \}\)\)/);
assert.match(workshop,/if\(saved&&saved\.channel_id===definition\.channel_id\)value=await updateSavedResearch/);
assert.match(readFileSync(new URL('../application/frontend/src/explorer/ResearchWorkshopPage.tsx',import.meta.url),'utf8'),/sourceMissing=\{study\.sourceMissing\}/);
console.log('Research graphs: typed edges, no result snapshots, portable Saved/backup and definition-only migration pass.');

// Render the operation component.
// Summary/calendar and page blocks share one note; null/missing adds no false warning.
const overview=controls;
function operationFixture(kind,limit) {
  const now=new Date().toISOString(),until=new Date(Date.now()+90000).toISOString();
  const result=kind===operations.summaryOperation?{population:0,metrics:Object.fromEntries(operations.metrics.map(metric=>[metric,
    {definition_epoch:null,state:'no_known_values',known_count:0,missing_count:0,total:null,mean:null,median:null,q1:null,q3:null,min:null,max:null,buckets:[]}]))}
    :kind===operations.calendarOperation?{population:0,known_count:0,year:2026,time_zone:'UTC',days:[],hours:[],weekdays:[]}
    :{metric:'views',definition_epoch:null,method:'last_recorded_channel_observation_per_bucket_v1',hours:24,bucket_hours:1,observations:0,points:[]};
  const source=kind===operations.historyOperation?research.channelSource:research.videoSource;
  return lifetime.captureSourceLifetime({version:1,operation:kind,operation_fingerprint:'f'.repeat(64),
    context:{source_id:source,evaluated_at:now,valid_before:until,consistency:'current',
      ...(limit===undefined?{}:{observation_age_limit_seconds:limit})},
    coverage:{status:'complete',reasons:{}},result,source_read_at:now,source_expires_at:until},undefined,performance.now());
}
for(const kind of [operations.summaryOperation,operations.calendarOperation]) {
  for(const limit of [604800,0,null,undefined]) {
    const data=operationFixture(kind,limit),parsed=operations.parseOperation(data,kind,research.videoSource);
    const html=renderToStaticMarkup(React.createElement(overview.OperationDisplay,{value:parsed}));
    const shared=renderToStaticMarkup(React.createElement(ageNote.ObservationAgeLimitNote,{limit}));
    if(limit===604800){assert.match(html,/Only videos up to 7 days old/);assert.ok(html.includes(shared));}
    else if(limit===0){assert.match(html,/No video is refreshed often enough/);assert.ok(html.includes(shared));}
    else assert.doesNotMatch(html,/refreshed often enough|Only videos up to/);
  }
  for(const invalid of [-1,0.5,'604800',false,NaN,Infinity])
    assert.throws(()=>operations.parseOperation(operationFixture(kind,invalid),kind,research.videoSource));
}
const history=operations.parseOperation(operationFixture(operations.historyOperation,null),operations.historyOperation,research.channelSource);
assert.doesNotMatch(renderToStaticMarkup(React.createElement(overview.OperationDisplay,{value:history})),/refreshed often enough|Only videos up to/);
console.log('Operation parsing and rendered summary/calendar use the shared age note; 0, null and channel history stay distinct.');

// The workshop page renders the study itself (real HTML, not source text). A new study has no source yet:
// Run and Save wait for one; no "Back to catalogue" on the page.
{
  const handoffModule=compiled('workshopHandoff.ts',{'./savedQueryDefinition':definition,'./researchQuery':research,'./researchGraph':graph,'./savedViews':views,'./savedWork':saved});
  const model=compiled('workshopModel.ts',{'./api':workshopApi,'./researchGraph':graph,'./researchQuery':research,'./workshopHandoff':handoffModule});
  const conditionsModule=compiled('ResearchConditions.tsx',{'react/jsx-runtime':jsxRuntime,'./researchQuery':research});
  const blocks=compiled('WorkshopBlocks.tsx',{'react/jsx-runtime':jsxRuntime,react:React,'./api':workshopApi,'../sourceLifetimeReact':{useSourceExpiry(){}},'./ObservationAgeLimitNote':ageNote,
    './ResearchConditions':conditionsModule,'./ResearchControls':controls,'./ResearchRows':rowsModule,'./researchGraph':graph,'./researchQuery':research,'./workshopModel':model});
  const workshopModule=compiled('ResearchWorkshop.tsx',{'react/jsx-runtime':jsxRuntime,react:React,'./api':workshopApi,'./ResearchConditions':conditionsModule,'./ResearchControls':controls,
    './WorkshopBlocks':blocks,'./researchQuery':research,'./researchGraph':graph,'./savedViews':views,'./savedQueryDefinition':definition,'./savedWork':saved,'./workshopModel':model,
    './workshopHandoff':handoffModule,'./researchWorkshop.css':{}});
  const render=props=>renderToStaticMarkup(React.createElement(workshopModule.ResearchWorkshop,props));
  const fresh=render({initial:graph.newStudy(),sources:{folder:[],label:[],channel:[],collection:[]},pageActions:React.createElement('button',null,'New study')});
  assert.match(fresh,/<button disabled="">Run research<\/button>/);
  assert.match(fresh,/<button disabled="">Save research<\/button>/);
  assert.match(fresh,/Choose a source in the Source block to run or save this study\./);
  assert.match(fresh,/<button>New study<\/button>/);
  assert.doesNotMatch(fresh,/Back to catalogue|from the catalogue|catalogue source/);
  const chosen=graph.withScope(graph.newStudy(),{kind:'folder',id:'22222222-2222-4222-8222-222222222222'});
  const ready=render({initial:chosen,initialName:'Folder study',initialNotice:'Opened from Library.',sources:{folder:[{id:'22222222-2222-4222-8222-222222222222',name:'Cooking'}],label:[],channel:[],collection:[]}});
  assert.match(ready,/<button>Run research<\/button>/);assert.match(ready,/<h1>Folder study<\/h1>/);assert.match(ready,/Opened from Library\./);
  assert.match(ready,/<option value="22222222-2222-4222-8222-222222222222" selected="">Cooking<\/option>/);
  assert.match(render({initial:chosen,apiAvailable:false}),/<button disabled="">Run research<\/button>.*Stored data is temporarily unavailable\./s);
  // ?settings=fields opens Collection fields; the failure text names the panel that retries.
  const settings=compiled('ResearchFieldSettings.tsx',{'react/jsx-runtime':jsxRuntime,react:React,'./api':{requestJson(){}},'./researchFieldPreferences':compiled('researchFieldPreferences.ts')});
  const panel=initialOpen=>renderToStaticMarkup(React.createElement(settings.ResearchFieldSettings,{changed(){},initialOpen}));
  assert.match(panel(true),/<button aria-expanded="true">Collection fields<\/button><div><h2>Choose what to collect<\/h2>/);
  assert.match(panel(false),/<button aria-expanded="false">Collection fields<\/button><\/section>/);
  const settingsSource=readFileSync(new URL('../application/frontend/src/explorer/ResearchFieldSettings.tsx',import.meta.url),'utf8');
  assert.match(settingsSource,/Reopen Collection fields to retry\./);assert.doesNotMatch(settingsSource,/Reopen the catalogue/);
}
console.log('Workshop page: unchosen source waits, no catalogue detour, Collection fields opens from its address.');

// At phone width (375 px) the six-column Collection fields table made the canvas page 513 px wide and cut
// off Shorts and Streams. The real panel, after its settings read, puts the table in its own scrolling frame, and the
// canvas keeps one column no wider than the page.
{
  const { reactHookHost, flush } = await import('./react_hook_host.mjs');
  const host = reactHookHost(), reads = [];
  const preferences = { revision: 1, pending_import: null, document: { version: 1, defaults_profile: 'public-current-v1',
    lifecycle_profile: 'public-696h-v1', workspace: {}, sources: {}, description_setup: 'pending' } };
  const panel = compiled('ResearchFieldSettings.tsx', { 'react/jsx-runtime': jsxRuntime, react: host.React,
    './api': { requestJson: async path => { reads.push(path); return { preferences }; } }, './researchFieldPreferences': compiled('researchFieldPreferences.ts') });
  host.mount(() => panel.ResearchFieldSettings({ changed() {}, initialOpen: true }), null);
  await flush();
  assert.deepEqual(reads, ['/research/fields']);
  const markup = renderToStaticMarkup(host.current);
  assert.match(markup, /<div class="research-field-table"><table><thead><tr><th>Field<\/th><th>Workspace default<\/th><th>Long-form<\/th><th>Shorts<\/th><th>Streams<\/th><\/tr><\/thead>/);
  assert.match(markup, /<\/tbody><\/table><\/div><p>Off stops storing/);
  assert.equal((markup.match(/<select /g) ?? []).length, 8, 'two fields × (workspace default + three formats), all inside the frame');
  const css = file => readFileSync(new URL('../application/frontend/src/explorer/' + file, import.meta.url), 'utf8');
  assert.match(css('researchCatalogue.css'), /\.research-field-settings \.research-field-table \{ overflow-x: auto; max-width: 100%; margin: 1rem 0; \}/);
  assert.match(css('canvas/canvas.css'), /\.research-workshop\.canvas-workshop \{[^}]*display: grid; grid-template-columns: minmax\(0, 1fr\);/);
  host.unmount();
}
console.log('the Collection fields table scrolls in its own frame; the canvas column never outgrows the page.');
