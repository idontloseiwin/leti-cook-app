import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
import * as source from '../application/frontend/src/sourceLifetime.ts';
import {parseSidebarData} from '../application/frontend/src/explorer/contracts.ts';
import {retainFreshSidebarSources} from '../application/frontend/src/sidebarUserState.ts';
const {captureSourceLifetime: capture, sourceExpired: expired, freshSourceRows: fresh, sourceLifetimeParser: parse, sourceOwnExpired: ownExpired} = source;
const start = Date.parse('2026-10-09T00:00:00Z'), stamp = ms => new Date(start + ms).toISOString();
const headers = (expiry = stamp(1000), read = stamp(0)) => ({get: name => name === 'X-Source-Expires-At' ? expiry : name === 'X-Source-Read-At' ? read : null});
const rows = capture({source_read_at: stamp(0), source_expires_at: stamp(1000), videos: [
  {video_id:'old', title:'old title', source_expires_at:stamp(1000)},
  {video_id:'fresh', title:'fresh title', source_expires_at:stamp(10000)},
]}, headers(), 100);
assert.deepEqual(fresh(rows.videos, start + 1000, 1100).map(row => row.video_id), ['fresh'], 'Equality expires only its own row.');
assert.equal(expired(rows.videos[1], start + 1000, 1100), false, 'Earlier envelope is not a cap on a proven independent row.');
assert.equal(expired(rows.videos[1], start - 86400000, 10100), true, 'A backward browser clock cannot extend the monotonic server-time budget.');
assert.equal(expired(rows.videos[0], start + 5000, 100), true, 'The wall-clock bound also masks rows after sleep.');
assert.equal(rows.videos.length, 2, 'Presentation filtering never mutates a frozen transport page.');
const decoded = parse(value => ({videos:value.videos.map(row => ({video_id:row.video_id,title:row.title}))}))(rows);
assert.deepEqual(fresh(decoded.videos, start + 1000, 1100).map(row => row.video_id), ['fresh'], 'DTO projection keeps each original acquisition clock even when it drops the wire field.');
const transferred = source.transferSourceLifetime(rows.videos[1], {video_id:'fresh', title:'fresh title'});
assert.equal(expired(transferred, start - 86400000, 10100), true, 'A copied inspector record never starts a new lifetime.');
for (const proof of [null, 'bad', '2026-02-30T00:00:00Z', '2030-01-01']) {
  const result = capture({source_read_at:stamp(0), source_expires_at:stamp(1000), videos:[{video_id:'x',source_expires_at:proof}]},headers(),100);
  assert.equal(expired(result.videos[0],start + 1000,1100),true, 'Null/invalid proof cannot bypass the parent.');
}
const legacy = capture({videos:[{video_id:'x'}]},headers(),100);
assert.equal(expired(legacy.videos[0],start + 1000,1100),true,'Legacy rows without their own witness inherit the envelope.');
const inconsistent = capture({source_read_at:stamp(0),source_expires_at:stamp(2000),videos:[{source_expires_at:stamp(99999)}]},headers(),100);
assert.equal(expired(inconsistent.videos[0],start,100),true,'Inconsistent body/header clocks fail closed.');
const local = source.withoutSourceLifetime({video_id:rows.videos[0].video_id, selected:true});
assert.equal(expired(local,start+100000,100000),false);
const envelope = source.sourceContainer({...rows,videos:fresh(rows.videos,start+1000,1100)});
assert.equal(expired(envelope,start+1000,1100),false);
assert.equal(ownExpired(envelope,start+1000,1100),true,'Expired counts/coverage still have their original guard.');

// Unavailable metadata is valid for one channel, not a reason to discard
// every other channel's fresh source fields.
{
  const channel = (id, title) => ({channel_id:id,title,avatar_url:null,
    subscribers:null,video_count:null,folder_id:'research',label_keys:['watch'],
    added_at:stamp(-10000)});
  const payload = {channels:[
    ...Array.from({length:848},(_,i)=>channel(`channel-${i}`,`Channel ${i}`)),
    channel('unavailable',null),
  ],folders:[{id:'research',name:'Research'}],
    labels:[{label_key:'watch',name:'Watch',label_type:'custom'}]};
  const parsed = parseSidebarData(payload);
  assert.equal(parsed.channels.length,849);
  assert.ok(parsed.channels.slice(0,848).every((row,i)=>row.title===`Channel ${i}`));
  assert.equal(parsed.channels[848].title,null);
  assert.equal(parsed.channels[848].channel_id,'unavailable');
  assert.deepEqual(parsed.channels[848].label_keys,['watch']);
  assert.deepEqual(parsed.folders,payload.folders);
  assert.deepEqual(parsed.labels,payload.labels);
  assert.equal(parseSidebarData({channels:[{channel_id:'local'}],folders:[],labels:[]}).channels[0].channel_id,'local');
  for (const title of [42,{},[],false]) {
    assert.throws(()=>parseSidebarData({...payload,channels:[channel('invalid',title)]}),
      /channels\[0\]\.title/, 'Wrong types still fail validation.');
  }
  assert.throws(()=>parseSidebarData({...payload,channels:[{...channel('invalid',null),label_keys:[42]}]}),
    /channels\[0\]\.label_keys/);

  const oldNow=Date.now;
  Date.now=()=>start+1000;
  try {
    const timed = capture({source_read_at:stamp(0),source_expires_at:stamp(1000),
      channels:[{...channel('fresh','Fresh name'),source_expires_at:stamp(10000)},
        {...channel('unavailable',null),source_expires_at:null},
        {...channel('expired','Expired name'),source_expires_at:stamp(1000)}],
      folders:payload.folders,labels:payload.labels},headers(),performance.now());
    const decoded = parse(parseSidebarData)(timed);
    const visible = retainFreshSidebarSources(decoded);
    assert.equal(visible.channels[0].title,'Fresh name');
    assert.equal(expired(visible.channels[0],start+10000,performance.now()),true,
      'Parsing never renews a fresh channel acquisition clock.');
    assert.equal(visible.channels[1].channel_id,'unavailable');
    assert.equal(visible.channels[2].title,undefined);
    assert.equal(visible.channels[2].channel_id,'expired');
    assert.equal(visible.channels[2].folder_id,'research');
    assert.equal(visible.channels[2].added_at,stamp(-10000));
    assert.deepEqual(visible.channels[2].label_keys,['watch']);
    assert.equal(decoded.channels[2].title,'Expired name','Presentation does not mutate the transport.');
  } finally {Date.now=oldNow;}
}

const compile = path => ts.transpileModule(readFileSync(new URL(path, import.meta.url),'utf8'), {compilerOptions:{
  target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,
}}).outputText;
const moduleWith = (path, imports, globals={}) => {
  const exports={};
  new Function('require','exports',...Object.keys(globals),compile(path))(name=>{
    assert.ok(name in imports, name); return imports[name];
  },exports,...Object.values(globals));
  return exports;
};
// One nearest-deadline timer, immediate masking, a visible GET refresh, and no
// repeated refresh flood when the server keeps returning an expired envelope.
{
  let wall=start, mono=100, id=0, masks=0, reads=0;
  const timers=new Map(),events=new Map(),effects=[], refs=[];
  const document={hidden:false,addEventListener:(key,fn)=>events.set(key,fn),removeEventListener:key=>events.delete(key)};
  const fakeDate=class extends Date {static now(){return wall;}};
  const fakePerformance={now:()=>mono};
  const hooks={useRef:value=>{const ref={current:value};refs.push(ref);return ref;},useEffect:fn=>effects.push(fn)};
  const mod=moduleWith('../application/frontend/src/sourceLifetimeReact.ts', {react:hooks,'./sourceLifetime.ts':source},{
    Date:fakeDate,performance:fakePerformance,document,window:{addEventListener:document.addEventListener,removeEventListener:document.removeEventListener},
    setTimeout:(fn,ms)=>{const key=++id;timers.set(key,{fn,ms});return key;},clearTimeout:key=>timers.delete(key),
  });
  mod.useSourceExpiry(rows,()=>masks++,()=>reads++);
  const stop=effects[0]();
  assert.equal(timers.size,1);
  wall=start+1000;mono=1100; const timer=[...timers][0];timers.delete(timer[0]);timer[1].fn();
  assert.equal(masks,1);assert.equal(reads,1);assert.ok([...timers.values()].some(timer=>timer.ms>8000),'A later independent row retains a timer.');
  events.get('visibilitychange')();events.get('pageshow')();
  assert.equal(reads,1,'Visibility bursts are coalesced.');
  document.hidden=true;wall=start+10000;mono=10100; for(const [key,timer] of [...timers]){timers.delete(key);timer.fn();}
  assert.equal(masks,2);assert.equal(reads,1,'Expired rows are masked while hidden, without starting a read.');
  document.hidden=false;events.get('visibilitychange')();
  assert.equal(reads,2,'Returning to the tab retries a read.');
  stop();assert.equal(timers.size,0);assert.equal(events.size,0);
}
// History projections retain both endpoint clocks and never connect points
// across a removed interval or keep a stale copied "latest interval".
{
  const history=moduleWith('../application/frontend/src/explorer/videoHistory.ts',{
    './contracts':{ExplorerContractError:Error,utcTimestampMicroseconds:()=>null},
    './format':{formatCount:String},'./localTime':{formatExactLocal:String},'../sourceLifetime':source,
  });
  const oldNow=Date.now; Date.now=()=>start+1000;
  try {
    const payload=capture({source_read_at:stamp(0),source_expires_at:stamp(1000),status:'ready',reason:null,
      window:{from_at:stamp(-1000),to_at:stamp(10000)},coverage:{point_count:2,valid_view_count:2,missing_view_count:0,duplicate_timestamp_count:0},
      points:[{recorded_at:stamp(-1000),views:'1',source_expires_at:stamp(1000)},{recorded_at:stamp(0),views:'2',source_expires_at:stamp(10000)}],
      intervals:[{from_at:stamp(-1000),to_at:stamp(0),actual_interval_seconds:'1',delta_views:'1',views_per_day:'86400',source_expires_at:stamp(1000)}],
      latest_interval:{from_at:stamp(-1000),to_at:stamp(0),actual_interval_seconds:'1',delta_views:'1',views_per_day:'86400',source_expires_at:stamp(1000)},
    },headers(),performance.now());
    const view=history.videoHistorySeries(payload);
    assert.equal(view.points.length,1);assert.equal(view.points[0].value,'2');
    assert.equal(view.intervals.length,0);assert.equal(view.latest_interval,null);
    assert.equal(payload.points.length,2,'Wire validation/provenance stays intact.');
  } finally {Date.now=oldNow;}
}

 // Expired review evidence is removed in both list and open detail. Operator
 // decisions and a denied write stay unchanged; no client-side permission lift.
 {
   const reviews=moduleWith('../application/frontend/src/videoTypeReviews.ts',{
     './serverText.ts':{serverErrorMessage:()=>''},'./sourceLifetime':source,
   });
   // Retained identity and decisions do not prove current write eligibility.
   const unavailable={case_id:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',video_id:'aaaaaaaaaaa',channel_id:'UC'+'a'.repeat(22),
     source_detached_at:null,blocked_reason:'source_unavailable',can_decide:false,
     observed_types:[],canonical_type:'long',state:'open',revision:2,
     first_seen_at:stamp(-1000),last_seen_at:stamp(0),decision:null};
   assert.equal(reviews.parseVideoTypeCase(unavailable).can_decide,false);
   const saved={...unavailable,state:'resolved',decision:{video_type:'long',reason:'Owner decision kept',decided_at:stamp(0),revision:2}};
   assert.equal(reviews.parseVideoTypeCase(saved).decision.reason,'Owner decision kept');
   assert.throws(()=>reviews.parseVideoTypeCase({...unavailable,can_decide:true}),/contradictory decision eligibility/);
   assert.throws(()=>reviews.parseVideoTypeCase({...unavailable,source_detached_at:stamp(0)}),/contradictory source availability/);
   const row=capture({source_read_at:stamp(0),source_expires_at:stamp(1000),
     case_id:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',video_id:'aaaaaaaaaaa',channel_id:'UC'+'a'.repeat(22),
     source_detached_at:null,blocked_reason:'channel_conflict',can_decide:false,
     observed_types:['short'],canonical_type:'short',state:'open',revision:1,
     first_seen_at:stamp(-1000),last_seen_at:stamp(0),
     decision:{video_type:'long',reason:'Owner decision kept',decided_at:stamp(0),revision:1},
   },headers(),performance.now());
   const jsx={jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props}),Fragment:'fragment'};
   const flatten=node=>!node?[]:Array.isArray(node)?node.flatMap(flatten):typeof node!=='object'?[node]:[node,...flatten(node.props?.children)];
   const text=tree=>flatten(tree).filter(value=>typeof value==='string').join(' ');
   let detail=false,index=0;
   const hooks={
     useState(initial){const at=index++;return [detail&&at===0?{case:row,history:[],next_before_revision:null}:initial&&typeof initial==='object'&&'reviewChanged' in initial?{page:{cases:[row],next_cursor:null},reviewChanged:false}:typeof initial==='function'?initial():initial,()=>{}];},
     useRef:value=>({current:value}),useEffect:()=>{},useCallback:fn=>fn,
   };
   const panel=moduleWith('../application/frontend/src/components/VideoTypeReviewPanel.tsx',{
     react:hooks,'react/jsx-runtime':jsx,'./HiddenVideoIdentities':{HiddenVideoIdentities:()=>null},
     '../videoTypeReviews':reviews,'../sourceLifetime':source,'../sourceLifetimeReact':{useSourceExpiry:()=>{}},
   });
   const oldNow=Date.now;Date.now=()=>start+1000;
   try {
     const list=panel.VideoTypeReviewPanel(),words=text(list);
     assert.match(words,/Owner decision kept/);
     assert.match(words,/Current evidence unavailable/);
     assert.doesNotMatch(words,/Channel identity conflict|Current: Short/);
     detail=true;index=0;
     const tree=panel.VideoTypeReviewDetails({initialCase:row,initialMode:'edit',onCase:()=>{},onClose:()=>{}});
     assert.match(text(tree),/Current evidence unavailable/);
     assert.doesNotMatch(text(tree),/Channel identity also conflicts/);
     const select=flatten(tree).find(node=>node.type==='select');
     assert.equal(select.props.disabled,true,'Expiry must never enable a denied write.');
   } finally {Date.now=oldNow;}
 }
console.log('Open-tab lifetime: independent rows, acquisition clocks, exact boundary, fail-closed proofs, projected history, bounded refresh and visibility passed.');
