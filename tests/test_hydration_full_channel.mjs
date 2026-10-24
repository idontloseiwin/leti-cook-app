import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
import React from '../application/frontend/node_modules/react/index.js';
import { renderToStaticMarkup } from '../application/frontend/node_modules/react-dom/server.node.js';
import * as jsxRuntime from '../application/frontend/node_modules/react/jsx-runtime.js';

const read = name => readFileSync(new URL(`../application/frontend/src/explorer/${name}`, import.meta.url), 'utf8');
function compiled(source, globals = {}) {
  const { outputText, diagnostics } = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
    reportDiagnostics: true,
  });
  assert.deepEqual(diagnostics, []);
  const exports = {};
  new Function('require', 'exports', ...Object.keys(globals), outputText)(name => {
    if (name === 'react/jsx-runtime') return jsxRuntime;
    throw new Error(`unexpected import ${name}`);
  }, exports, ...Object.values(globals));
  return exports;
}
function declaration(file, name) {
  const source = ts.createSourceFile(file, read(file), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let result;
  function visit(node) {
    if ((ts.isFunctionDeclaration(node) || ts.isVariableDeclaration(node)) && node.name?.getText(source) === name) result = node;
    ts.forEachChild(node, visit);
  }
  visit(source);
  assert.ok(result, `missing ${name}`);
  return ts.isVariableDeclaration(result)
    ? `export const ${result.getText(source)};`
    : `export ${result.getText(source).replace(/^export\s+/, '')}`;
}
const contracts = compiled(read('contracts.ts'));
const hydration = compiled(read('hydration.ts'));
const channelId = 'UC' + 'a'.repeat(22);
const unknownEstimate = () => ({items: null, pages: null, quota_units: null, estimate_kind: 'unknown', items_lower_bound: 0, items_upper_bound: null});
const payload = (overrides = {}) => ({
  contract_version: 2, channel_id: channelId, status: 'confirmation_required', phase: 'inventory',
  current_profile_ready: false, inventory_ready: false,
  progress: {processed: 0, total: null, percent: null, discovered: 0},
  estimate: unknownEstimate(), confirmation_required: true, action_required: 'confirm', job: null,
  ...overrides,
});
const parse = value => contracts.parseExplorerHydration(value, channelId);
const knownEstimate = kind => ({items: 10001, pages: 201, quota_units: 402, estimate_kind: kind,
  items_lower_bound: kind === 'exact_workset' ? 10001 : 9000, items_upper_bound: 10001});

test('contract2 admits unknown manual confirmation without inventing counts', () => {
  const value = payload();
  assert.equal(parse(value), value);
  assert.equal(value.progress.total, null);
  assert.equal(value.estimate.quota_units, null);
});

test('contract2 requires discovered count and all estimate metadata', () => {
  for (const key of ['estimate_kind', 'items_lower_bound', 'items_upper_bound']) {
    const value = payload(); delete value.estimate[key];
    assert.throws(() => parse(value), contracts.ExplorerContractError);
  }
  const value = payload(); delete value.progress.discovered;
  assert.throws(() => parse(value), contracts.ExplorerContractError);
  for (const discovered of [-1, 0.5, true, null, '3']) {
    assert.throws(() => parse(payload({progress:{processed:0,total:null,percent:null,discovered}})), contracts.ExplorerContractError);
  }
});

test('unknown totals reject percentages and unknown estimates reject fabricated zeros', () => {
  for (const percent of [0, 50, 100]) {
    assert.throws(() => parse(payload({progress:{processed:0,total:null,percent,discovered:10001}})), contracts.ExplorerContractError);
  }
  for (const field of ['items', 'pages', 'quota_units', 'items_upper_bound']) {
    const value = payload(); value.estimate[field] = 0;
    assert.throws(() => parse(value), contracts.ExplorerContractError);
  }
});

test('known estimates validate upper and exact bounds; frozen progress is exact', () => {
  for (const kind of ['upper_bound', 'exact_workset']) {
    assert.equal(parse(payload({estimate:knownEstimate(kind)})).estimate.estimate_kind, kind);
  }
  const value = payload({status:'running', phase:'hydrate_videos', confirmation_required:false, action_required:null,
    progress:{processed:500,total:10001,percent:5,discovered:10001}, estimate:knownEstimate('exact_workset')});
  assert.equal(parse(value).progress.total, 10001);
  for (const estimate of [
    {...knownEstimate('upper_bound'),items_upper_bound:10000},
    {...knownEstimate('upper_bound'),items_lower_bound:10002},
    {...knownEstimate('exact_workset'),items_lower_bound:10000},
    {...knownEstimate('exact_workset'),pages:null},
  ]) assert.throws(() => parse(payload({estimate})), contracts.ExplorerContractError);
  assert.throws(() => parse({...value,progress:{...value.progress,processed:10002}}), contracts.ExplorerContractError);
});

test('contract1 remains numeric and other Explorer contracts remain version1', () => {
  const value = payload({contract_version:1, progress:{processed:0,total:20,percent:0}, estimate:{items:20,pages:1,quota_units:2}});
  assert.equal(parse(value).contract_version, 1);
  assert.throws(() => parse({...value,progress:{processed:0,total:null,percent:null}}), contracts.ExplorerContractError);
  for (const version of [0, 3, '2', true]) assert.throws(() => parse(payload({contract_version:version})), contracts.ExplorerContractError);
  assert.throws(() => contracts.parseExplorerCapabilities({contract_version:2,channel_explorer:{raw:'available'}}), contracts.ExplorerContractError);
});

test('full-channel completion does not open the Long profile gate', () => {
  const value = payload({status:'completed',phase:'verify',confirmation_required:false,action_required:null,
    progress:{processed:7,total:7,percent:100,discovered:7},estimate:null,
    all_initial_checks_complete:true,job:{status:'completed',phase:'verify'}});
  parse(value);
  assert.equal(hydration.hydrationInitialComplete(value), true);
  assert.equal(hydration.hydrationProfileReady(value), false);
  assert.equal(hydration.hydrationInitialBecameComplete(undefined,value), true);
  assert.throws(() => parse({...value,status:'ready',phase:'ready'}), contracts.ExplorerContractError);
});

test('a verified published Long subset opens while a premiere remains checked', () => {
  const value = payload({status:'ready',phase:'ready',confirmation_required:false,action_required:null,
    current_profile_ready:true,inventory_ready:true,all_initial_checks_complete:true,
    progress:{processed:2,total:2,percent:100,discovered:2},estimate:null,
    job:{status:'completed',phase:'verify',summary:{per_format:{long:{updated:1,checked:1,current_profile_ready:false}}}}});
  parse(value);
  assert.equal(hydration.hydrationInitialComplete(value), true);
  assert.equal(hydration.hydrationProfileReady(value), true);
  assert.equal(hydration.hydrationNeedsPolling(value), false);
});

const banner = compiled(declaration('ExplorerWorkspace.tsx', 'ExplorerHydrationBanner'), {
  ...hydration, formatCount: value => value === null ? '—' : value.toLocaleString('en-US'),
  videoNoun: value => value === 1 ? 'video' : 'videos',
  LoaderCircle: () => null, AlertTriangle: () => null, RefreshCw: () => null,
}).ExplorerHydrationBanner;

const publicationPresentation = await import('../application/frontend/src/explorer/publicationStatus.ts');
const localTime = compiled(read('localTime.ts'));
function renderPublications(videos, expired = false) {
  const state = [{videos,next_cursor:null},[undefined],false,false,0];
  const component = compiled(declaration('ChannelPublications.tsx', 'ChannelPublications'), {
    ...publicationPresentation, ...localTime,
    useState: () => [state.shift(), () => {}], useEffect: () => {}, useSourceExpiry: () => {},
    sourceExpired: () => expired,
    loadChannelPublications: () => { throw new Error('render must not start collection or an API request'); },
  }).ChannelPublications;
  return renderToStaticMarkup(React.createElement(component,{channelId,initialComplete:true}));
}
const pendingVideo = overrides => ({video_id:'abcdefghijk',video_type:'short',title:'Future <video>',
  publication_status:'upcoming',scheduled_start_at:null,checked_at:'2026-10-16T12:00:00Z',
  source_expires_at:'2026-11-14T12:00:00Z',...overrides});

test('publication list presents API stage and missing date without inferring type or a start', () => {
  const html = renderPublications([pendingVideo()]);
  assert.match(html,/Future &lt;video&gt;/);
  assert.match(html,/https:\/\/www.youtube.com\/watch\?v=abcdefghijk/);
  assert.match(html,/Shorts/);
  assert.match(html,/Upcoming/);
  assert.match(html,/Scheduled time not reported/);
  assert.match(html,/reaching a time does not confirm/);
  const unknown = renderPublications([pendingVideo({video_type:null})]);
  assert.match(unknown,/Type unconfirmed/);
  assert.doesNotMatch(unknown,/>Long</);
});

test('publication list preserves a reported date and hides expired observations', () => {
  const video = pendingVideo({scheduled_start_at:'2026-10-01T12:00:00Z'});
  const html = renderPublications([video]);
  assert.match(html,/dateTime="2026-10-01T12:00:00Z"/i);
  assert.match(html,/Upcoming/);
  assert.doesNotMatch(renderPublications([video],true),/Future &lt;video&gt;|watch\?v=abcdefghijk/);
});

const render = (value, youtubeVideoTotal = 0) => renderToStaticMarkup(React.createElement(banner, {
  state:{kind:'ready',payload:parse(value)},checking:false,youtubeVideoTotal,onConfirm:()=>{},onRetry:()=>{},
}));

test('unknown size renders a working manual action and no numeric progress bar', () => {
  const html = render(payload());
  assert.match(html, /Size unknown\. Confirm to load the full channel\./);
  assert.match(html, /Load full channel/);
  assert.doesNotMatch(html, /disabled|aria-valuenow|role="progressbar"|0%|0 estimated API units/);
});

test('10001 discovery confirmation continues the same job and shows distinct count', () => {
  const html = render(payload({job:{job_id:'same-job',status:'blocked'},
    progress:{processed:0,total:null,percent:null,discovered:10001}}));
  assert.match(html, /10,001 videos discovered; total size unknown/);
  assert.match(html, /Continue full channel/);
  assert.doesNotMatch(html, /aria-valuenow|of 10,001 loaded|disabled/);
});

test('blocked inventory explains lost checkpoint and labels a full restart honestly', () => {
  const value = payload({status:'blocked', confirmation_required:false, action_required:'retry',
    job:{error_code:'initial_inventory_restart_required'}});
  const html = render(value);
  assert.match(html, /read the channel lists from the beginning/);
  assert.match(html, /Already saved data is kept/);
  assert.match(html, />Restart loading</);
  assert.doesNotMatch(html, /Retry profile loading/);
});

test('blocked cause uses reviewed messages and never renders an arbitrary error', () => {
  const value = payload({status:'blocked', confirmation_required:false, action_required:'retry',
    job:{error_code:'youtube_quota_exhausted'}});
  assert.match(render(value), /quota resets/);
  assert.doesNotMatch(render(value), /Restart loading/);
  value.job.error_code = 'private-unreviewed-provider-message';
  assert.match(render(value), /Check Health/);
  assert.doesNotMatch(render(value), /private-unreviewed/);
});

test('frozen exact progress renders percentage and upper estimates are labelled', () => {
  const html = render(payload({status:'running',phase:'hydrate_videos',confirmation_required:false,action_required:null,
    progress:{processed:500,total:10001,percent:5,discovered:10001},estimate:knownEstimate('exact_workset')}));
  assert.match(html, /500 of 10,001 loaded/);
  assert.match(html, /aria-valuenow="5"/);
  assert.doesNotMatch(html, /total size unknown/);
  assert.match(render(payload({estimate:knownEstimate('upper_bound')})), /Up to 10,001 videos/);
});

test('inventory discovery retains the known estimate without pretending it is exact progress', () => {
  const html = render(payload({status:'running', confirmation_required:false, action_required:null,
    estimate:knownEstimate('upper_bound'),progress:{processed:0,total:null,percent:null,discovered:500}}));
  assert.match(html, /500 videos discovered; estimated up to 10,001 videos/);
  assert.doesNotMatch(html, /total size unknown|aria-valuenow|of 10,001 loaded/);
});

test('accepted initial quota estimate has its own label and validates its provenance', () => {
  const estimate = {...knownEstimate('upper_bound'), quota_units:811, quota_basis:'before_start'};
  assert.match(render(payload({estimate})), /811 API units estimated before starting/);
  for (const quota_basis of ['observed', 'current', null, true]) {
    assert.throws(() => parse(payload({estimate:{...estimate,quota_basis}})), contracts.ExplorerContractError);
  }
  assert.throws(() => parse(payload({estimate:{...unknownEstimate(),quota_basis:'before_start'}})), contracts.ExplorerContractError);
});

test('a channel above the YouTube 20,000-upload list says why older videos cannot load', () => {
  const html = render(payload({estimate:knownEstimate('upper_bound')}), 33334);
  assert.match(html, /YouTube gives apps only the latest 20,000 videos of a channel\. This channel has 33,334 on YouTube, so the oldest 13,334 cannot be loaded\./);
  assert.doesNotMatch(render(payload({estimate:knownEstimate('upper_bound')}), 20000), /latest 20,000/);
});

test('real API function sends null expected values and an operation UUID', async () => {
  const requestId='22222222-2222-4222-8222-222222222222';
  let request;
  const api = compiled(declaration('api.ts','confirmExplorerHydration'), {
    explorerHydrationPath:id=>`/channels/${id}/hydration`, parseExplorerHydration:contracts.parseExplorerHydration,
    requestJson:async(path,signal,options)=>{request={path,signal,...options};return payload();},
  });
  await api.confirmExplorerHydration(channelId, unknownEstimate(), requestId);
  assert.equal(request.method,'POST');
  assert.equal(request.path,`/channels/${channelId}/hydration`);
  assert.deepEqual(JSON.parse(request.body),{expected_items:null,expected_pages:null,expected_quota_units:null,
    retry_request_id:requestId,confirmed:true});
});

test('real workspace confirm handler retains operation UUID across uncertain result', async () => {
  const requests=[]; const ref={current:undefined}; let generated=0;
  const noOp=()=>{};
  const current=payload({job:{job_id:'existing-job'},progress:{processed:0,total:null,percent:null,discovered:10001}});
  const handler=compiled(declaration('ExplorerWorkspace.tsx','handleConfirmHydration'), {
    ...hydration,currentHydration:current,channelId,hydrationRetryRef:ref,hydrationPayloadRef:{current:current},
    newSavedWorkId:()=>{generated++;return '33333333-3333-4333-8333-333333333333';},
    setHydrationState:noOp,setFormatTotalsReloadToken:noOp,setProfileReloadToken:noOp,
    setCounterDynamicsReloadToken:noOp,setCatalogueReloadToken:noOp,setBaselineReloadToken:noOp,
    setHydrationRequestToken:noOp,setAnnouncement:noOp,
    confirmExplorerHydration:async(id,estimate,operation)=>{
      requests.push({id,estimate,operation});
      if(requests.length===1)throw new Error('uncertain acknowledgement');
      return payload({status:'queued',confirmation_required:false,action_required:null,job:current.job});
    },
  }).handleConfirmHydration;
  await handler(); await handler();
  assert.equal(requests.length,2);
  assert.equal(generated,1);
  assert.equal(requests[0].operation,requests[1].operation);
  assert.notEqual(requests[0].operation,current.job.job_id);
  assert.equal(ref.current,undefined);
});
