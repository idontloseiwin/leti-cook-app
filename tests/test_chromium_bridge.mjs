import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { normalizeAppOrigin, normalizeChannelUrl, permissionPattern } from '../extensions/chromium/shared.js';
import { addChannelInApp } from '../extensions/chromium/bridge.js';
import { createChannelBridge } from '../extensions/chromium/background.js';

const ORIGIN = 'http://127.0.0.1:8020';
const ID = `UC${'a'.repeat(22)}`;
const CHANNEL = `https://www.youtube.com/channel/${ID}`;
const MESSAGE = { type: 'add-channel', channelUrl: CHANNEL };
const SENDER = { id: 'test-extension', frameId: 0, tab: { id: 8 }, url: 'https://www.youtube.com/@example' };
const success = { status: 'added', channelId: ID, requiresAttention: false };
const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
const lookup = (channelId = null, videoId = null) => JSON.stringify({channelId, videoId});
const auth = (mode = 'local') => ({ configured: true, authenticated: true, access_mode: mode });

async function withPage(fetcher, run, origin = ORIGIN) {
  const before = Object.getOwnPropertyDescriptor(globalThis, 'location');
  const oldFetch = globalThis.fetch;
  Object.defineProperty(globalThis, 'location', { value: { origin }, configurable: true });
  globalThis.fetch = fetcher;
  try { return await run(); }
  finally {
    globalThis.fetch = oldFetch;
    if (before) Object.defineProperty(globalThis, 'location', before);
    else delete globalThis.location;
  }
}
function browser(overrides = {}) {
  const events = [], session = {}, local = { appOrigin: ORIGIN };
  let clock = 1000;
  const tab = { id: 77, status: 'complete', url: `${ORIGIN}/app-explorer-v3` };
  const api = {
    runtime: { id: SENDER.id, openOptionsPage: async () => events.push(['options']) },
    storage: {
      local: { setAccessLevel: async (value) => events.push(['local-access', value]), get: async () => ({ ...local }) },
      session: { setAccessLevel: async (value) => events.push(['session-access', value]),
        get: async () => structuredClone(session), set: async (value) => { Object.assign(session, value); events.push(['channelAddIntent' in value ? 'intent' : 'identity']); },
        remove: async (key) => { delete session[key]; events.push(['intent-removed']); } },
    },
    permissions: { contains: async (value) => { events.push(['permission', value]); return true; } },
    tabs: { query: async () => [tab], create: async (value) => { events.push(['create', value]); return tab; },
      get: async () => ({ ...tab }), update: async (id, value) => events.push(['update', id, value]) },
    scripting: { executeScript: async (value) => { events.push(['execute', value]); return [{ frameId: 0, result: { ...success } }]; } },
  };
  Object.assign(api, overrides);
  return { api, events, session, local, tab, advance: (ms) => { clock += ms; },
    dependencies: { now: () => clock, sleep: async (ms) => { clock += ms; } } };
}

test('application addresses accept only explicit loopback HTTP origins', () => {
  assert.equal(normalizeAppOrigin(`${ORIGIN}/`), ORIGIN);
  assert.equal(normalizeAppOrigin(' http://localhost:80 '), 'http://localhost');
  assert.equal(permissionPattern(ORIGIN), `${ORIGIN}/*`);
  for (const value of ['https://localhost', 'http://[::1]:8020', 'http://127.1:8020', 'http://2130706433:8020',
    'http://127.0.0.1.evil:8020', 'http://user@127.0.0.1:8020', `${ORIGIN}/auth`, `${ORIGIN}?q=1`,
    `${ORIGIN}#x`, 'http://localhost:0', 'http://localhost:65536', 'file:///tmp', null, {}]) {
    assert.equal(normalizeAppOrigin(value), null, String(value));
    assert.equal(permissionPattern(value), null, String(value));
  }
});

test('channel locators discard nonidentity fields but reject foreign and ambiguous sources', () => {
  assert.equal(normalizeChannelUrl(`${CHANNEL}/videos?view=0#x`), CHANNEL);
  assert.equal(normalizeChannelUrl('https://youtube.com/@A.name/shorts'), 'https://www.youtube.com/@A.name');
  assert.equal(normalizeChannelUrl('https://www.youtube.com/@%D0%9A%D0%B0%D0%BD%D0%B0%D0%BB'),
    'https://www.youtube.com/@%D0%9A%D0%B0%D0%BD%D0%B0%D0%BB');
  for (const value of ['https://evil.test/@example', 'https://www.youtube.com.evil/@example',
    'https://user@www.youtube.com/@example', 'https://www.youtube.com:443/@example',
    'https://www.youtube.com/watch?v=abcdefghijk', 'https://www.youtube.com/c/example',
    'https://www.youtube.com/@bad%2Fname', 'https://www.youtube.com/@bad%00name',
    'https://www.youtube.com/@bad%ZZ', 'https://www.youtube.com/@.bad', CHANNEL + '/arbitrary', null]) {
    assert.equal(normalizeChannelUrl(value), null, String(value));
  }
});

test('local and password use session check then one same-origin authenticated JSON command', async () => {
  for (const mode of ['local', 'password']) {
    const calls = [];
    const result = await withPage(async (path, init) => {
      calls.push([path, init]);
      return path === '/auth/session' ? json(auth(mode)) : json({ status: 'added', channel: { channel_id: ID, title: 'not returned' } });
    }, () => addChannelInApp(ORIGIN, CHANNEL, null));
    assert.deepEqual(result, success);
    assert.deepEqual(calls.map(([path]) => path), ['/auth/session', '/add-channel']);
    for (const [, init] of calls) {
      assert.equal(init.credentials, 'same-origin'); assert.equal(init.redirect, 'error');
      assert.equal(init.cache, 'no-store'); assert.ok(init.signal instanceof AbortSignal);
      assert.equal('Origin' in init.headers, false);
    }
    assert.equal(calls[1][1].method, 'POST');
    assert.deepEqual(JSON.parse(calls[1][1].body), { url: CHANNEL });
  }
});

test('unsigned, pending and malformed sessions never send a provider-triggering POST', async () => {
  for (const [state, expected] of [
    [{ configured: true, authenticated: false, access_mode: 'password' }, 'login_required'],
    [{ configured: false, authenticated: false, access_mode: 'pending' }, 'setup_required'],
    [{ configured: true, authenticated: true, access_mode: 'unknown' }, 'unavailable'],
    [{ configured: true, username: 'not authorization' }, 'unavailable'],
  ]) {
    let requests = 0;
    const result = await withPage(async (path) => { requests++; assert.equal(path, '/auth/session'); return json(state); },
      () => addChannelInApp(ORIGIN, CHANNEL, null));
    assert.deepEqual(result, { status: expected }); assert.equal(requests, 1);
  }
});

test('destination and canonical locator are checked inside the injected function', async () => {
  await withPage(async () => { assert.fail('untrusted document made request'); }, async () => {
    assert.deepEqual(await addChannelInApp(ORIGIN, CHANNEL, null), { status: 'rejected' });
  }, 'http://127.0.0.1:8021');
  await withPage(async () => { assert.fail('invalid locator made request'); }, async () => {
    assert.deepEqual(await addChannelInApp(ORIGIN, 'https://evil.test/channel/' + ID, null), { status: 'rejected' });
  });
  const calls = [];
  const result = await withPage(async (path) => {
    calls.push(path); globalThis.location.origin = 'http://localhost:8020'; return json(auth());
  }, () => addChannelInApp(ORIGIN, CHANNEL, null));
  assert.deepEqual(calls, ['/auth/session']); assert.deepEqual(result, { status: 'rejected' });
});

test('HTTP 200 error and invalid success are not advertised as added', async () => {
  const cases = [
    [json({ error: 'sensitive raw failure', code: 'channel_not_found' }), 'rejected'],
    [json({ status: 'added', channel: { channel_id: 'bad' } }), 'unconfirmed'],
    [json({ status: 'added', channel: { channel_id: ID }, hydration: { channel_id: `UC${'b'.repeat(22)}` } }), 'unconfirmed'],
    [json({ detail: { code: 'channel_add_rate_limited', message: 'raw' } }, 429), 'rate_limited'],
    [json({ code: 'authentication_required' }, 401), 'login_required'],
    [json({ code: 'setup_required' }, 409), 'setup_required'],
    [json({ code: 'hot_runtime_not_ready' }, 503), 'unavailable'],
    [json({ error: 'raw after possible commit' }, 500), 'unconfirmed'],
    [new Response('<html>raw</html>', { headers: { 'Content-Type': 'text/html' } }), 'unconfirmed'],
    [new Response('x'.repeat(65537), { headers: { 'Content-Type': 'application/json' } }), 'unconfirmed'],
  ];
  for (const [response, status] of cases) {
    const result = await withPage(async (path) => path === '/auth/session' ? json(auth()) : response,
      () => addChannelInApp(ORIGIN, CHANNEL, null));
    assert.deepEqual(result, { status });
  }
});

test('network loss after POST is uncertain and does not retry', async () => {
  const calls = [];
  const result = await withPage(async (path) => {
    calls.push(path); if (path === '/auth/session') return json(auth());
    throw new Error('synthetic network disconnect');
  }, () => addChannelInApp(ORIGIN, CHANNEL, null));
  assert.deepEqual(result, { status: 'unconfirmed' });
  assert.deepEqual(calls, ['/auth/session', '/add-channel']);
});

test('hydration confirmation is reported independently of successful addition', async () => {
  const result = await withPage(async (path) => path === '/auth/session' ? json(auth())
    : json({ status: 'restored', channel: { channel_id: ID }, hydration: { channel_id: ID, confirmation_required: true } }),
  () => addChannelInApp(ORIGIN, CHANNEL, null));
  assert.deepEqual(result, { status: 'restored', channelId: ID, requiresAttention: true });
});

test('worker rejects foreign messages before consulting config or opening an application', async () => {
  const fake = browser(); const worker = createChannelBridge(fake.api, fake.dependencies);
  for (const [message, sender] of [[{ ...MESSAGE, endpoint: '/delete' }, SENDER], [MESSAGE, { ...SENDER, id: 'other' }],
    [MESSAGE, { ...SENDER, frameId: 1 }], [MESSAGE, { ...SENDER, url: 'https://evil.test' }],
    [MESSAGE, { ...SENDER, url: 'http://www.youtube.com/@example' }], [MESSAGE, { ...SENDER, tab: null }]]) {
    assert.deepEqual(await worker.handle(message, sender), { status: 'rejected' });
  }
  assert.deepEqual(fake.events, []);
});

test('worker protects storage and dispatches exactly one isolated top-frame function', async () => {
  const fake = browser(); const worker = createChannelBridge(fake.api, fake.dependencies);
  assert.deepEqual(await worker.handle(MESSAGE, SENDER), success);
  assert.deepEqual(fake.events.slice(0, 2), [['local-access', { accessLevel: 'TRUSTED_CONTEXTS' }],
    ['session-access', { accessLevel: 'TRUSTED_CONTEXTS' }]]);
  const calls = fake.events.filter(([event]) => event === 'execute'); assert.equal(calls.length, 1);
  const injected = calls[0][1];
  assert.deepEqual(injected.target, { tabId: 77, frameIds: [0] }); assert.equal(injected.world, 'ISOLATED');
  assert.equal(injected.func, addChannelInApp); assert.deepEqual(injected.args, [ORIGIN, CHANNEL, null]);
  assert.equal(fake.session.channelAddIntent, undefined);
  assert.deepEqual(fake.session.channelIdentities, [{origin: ORIGIN, url: CHANNEL, id: ID, at: 1000}]);
});

test('missing host grant opens settings without sending or injecting', async () => {
  const fake = browser(); fake.api.permissions.contains = async () => false;
  assert.deepEqual(await createChannelBridge(fake.api, fake.dependencies).handle(MESSAGE, SENDER), { status: 'setup_required' });
  assert.ok(fake.events.some(([event]) => event === 'options'));
  assert.equal(fake.events.some(([event]) => event === 'execute' || event === 'intent'), false);
});

test('destination changes before injection fail closed', async () => {
  const fake = browser(); fake.api.tabs.get = async () => ({ ...fake.tab, url: 'http://localhost:8020/app-explorer-v3' });
  assert.deepEqual(await createChannelBridge(fake.api, fake.dependencies).handle(MESSAGE, SENDER), { status: 'unavailable' });
  assert.equal(fake.events.some(([event]) => event === 'execute'), false);
});

test('concurrent clicks share one in-flight operation', async () => {
  const fake = browser(); let resolveInjection, markStarted;
  const started = new Promise((resolve) => { markStarted = resolve; });
  fake.api.scripting.executeScript = () => { markStarted(); return new Promise((resolve) => { resolveInjection = resolve; }); };
  const worker = createChannelBridge(fake.api, fake.dependencies);
  const first = worker.handle(MESSAGE, SENDER); await started;
  assert.deepEqual(await worker.handle(MESSAGE, SENDER), { status: 'busy' });
  resolveInjection([{ frameId: 0, result: success }]); assert.deepEqual(await first, success);
});

test('a restarted worker reports an unfinished command before another explicit click may retry', async () => {
  const fake = browser(); fake.session.channelAddIntent = { until: 2000 };
  const worker = createChannelBridge(fake.api, fake.dependencies);
  assert.deepEqual(await worker.handle(MESSAGE, SENDER), { status: 'busy' });
  fake.advance(1001);
  assert.deepEqual(await worker.handle(MESSAGE, SENDER), { status: 'unconfirmed' });
  assert.equal(fake.events.some(([event]) => event === 'execute'), false);
  assert.deepEqual(await worker.handle(MESSAGE, SENDER), success);
});

test('uncertain execution retains an intent and never causes an internal replay', async () => {
  const fake = browser(); let attempts = 0;
  fake.api.scripting.executeScript = async () => { attempts++; throw new Error('document disappeared'); };
  const worker = createChannelBridge(fake.api, fake.dependencies);
  assert.deepEqual(await worker.handle(MESSAGE, SENDER), { status: 'unconfirmed' });
  assert.deepEqual(await worker.handle(MESSAGE, SENDER), { status: 'busy' });
  assert.equal(attempts, 1); assert.ok(fake.session.channelAddIntent);
});

test('login activates auth and attention activates app without replacing existing work', async () => {
  for (const result of [{ status: 'login_required' }, { ...success, requiresAttention: true }]) {
    const fake = browser(); fake.api.scripting.executeScript = async () => [{ frameId: 0, result }];
    assert.deepEqual(await createChannelBridge(fake.api, fake.dependencies).handle(MESSAGE, SENDER), result);
    const update = fake.events.find(([event]) => event === 'update');
    assert.deepEqual(update, result.status === 'login_required'
      ? ['update', 77, { url: `${ORIGIN}/auth`, active: true }] : ['update', 77, { active: true }]);
  }
});

test('tracking lookup reads saved identities without a POST or returning user work', async () => {
  const calls=[];
  const result=await withPage(async(path,init)=>{
    calls.push([path,init.method]);
    return path==='/auth/session'?json(auth()):json({channels:[{channel_id:ID,folder_id:'private'}],labels:['private']});
  },()=>addChannelInApp(ORIGIN,CHANNEL,lookup()));
  assert.deepEqual(calls,[['/auth/session','GET'],['/sidebar-user-data','GET']]);
  assert.deepEqual(result,{status:'exists',channelId:ID});
});

test('known video mapping confirms tracking; missing video, invalid shape and unavailable app do not', async () => {
  const videoId='abcdefghijk';
  for (const [payload,expected] of [
    [{contract_version:1,video_id:videoId,status:'matched',channel_id:ID}, {status:'exists',channelId:ID}],
    [{contract_version:1,video_id:videoId,status:'not_tracked',channel_id:null},{status:'unknown'}],
    [{contract_version:1,video_id:'other',status:'matched',channel_id:ID},{status:'unavailable',diagnostic:'A23'}],
  ]) {
    const result=await withPage(async(path,init)=>{
      assert.equal(init.method,'GET');
      return path==='/auth/session'?json(auth()):json(payload);
    },()=>addChannelInApp(ORIGIN,'https://www.youtube.com/@owner',lookup(null,videoId)));
    assert.deepEqual(result,expected);
  }
  const result=await withPage(async()=>{throw new Error('offline');},
    ()=>addChannelInApp(ORIGIN,CHANNEL,lookup()));
  assert.deepEqual(result,{status:'unavailable',diagnostic:'A01'});
});

test('read worker is bounded and passive, caches briefly, rejects stale video identity', async () => {
  const fake=browser();
  fake.api.scripting.executeScript=async value=>{fake.events.push(['execute',value]);return [{frameId:0,result:{status:'exists',channelId:ID}}];};
  const worker=createChannelBridge(fake.api,fake.dependencies);
  const message={type:'channel-status',channelUrl:CHANNEL,videoId:null};
  assert.deepEqual(await worker.handle(message,SENDER),{status:'exists',channelId:ID});
  assert.deepEqual(await worker.handle(message,SENDER),{status:'exists',channelId:ID});
  assert.equal(fake.events.filter(([key])=>key==='execute').length,1);
  assert.equal(fake.events.some(([key])=>['intent','create','options','update'].includes(key)),false);
  assert.deepEqual(await worker.handle({...message,videoId:'abcdefghijk'},SENDER),{status:'rejected'});
  fake.advance(30001);
  await worker.handle(message,SENDER);
  assert.equal(fake.events.filter(([key])=>key==='execute').length,2);
  fake.api.tabs.query=async()=>[]; fake.advance(30001);
  assert.deepEqual(await worker.handle(message,SENDER),{status:'app_closed'});
  assert.equal(fake.events.some(([key])=>['intent','create','options','update'].includes(key)),false);
});

test('a hidden/missing known channel is not marked tracked, auth failure never issues a lookup', async () => {
  assert.deepEqual(await withPage(async path=>path==='/auth/session'?json(auth()):json({channels:[]}),
    ()=>addChannelInApp(ORIGIN,CHANNEL,lookup())),{status:'not_tracked'});
  let calls=0;
  assert.deepEqual(await withPage(async path=>{calls++;assert.equal(path,'/auth/session');return json({configured:true,authenticated:false,access_mode:'password'});},
    ()=>addChannelInApp(ORIGIN,CHANNEL,lookup())),{status:'login_required'});
  assert.equal(calls,1);
});

test('a stale read completing after an add is not cached over the new result', async () => {
  const fake = browser();
  let finishRead, reads = 0;
  fake.api.scripting.executeScript = async value => {
    if (value.args[2] === null) return [{frameId: 0, result: success}];
    reads++;
    if (reads === 1) return new Promise(resolve => { finishRead = resolve; });
    return [{frameId: 0, result: {status: 'exists', channelId: ID}}];
  };
  const worker = createChannelBridge(fake.api, fake.dependencies);
  const message = {type: 'channel-status', channelUrl: CHANNEL, videoId: null};
  const pending = worker.handle(message, SENDER);
  await new Promise(setImmediate);
  assert.deepEqual(await worker.handle(MESSAGE, SENDER), success);
  finishRead([{frameId: 0, result: {status: 'not_tracked'}}]);
  await pending;
  assert.deepEqual(await worker.handle(message, SENDER), {status: 'exists', channelId: ID});
  assert.equal(reads, 2);
});

test('passive reads allow at most two concurrent injections', async () => {
  const fake = browser();
  const pending = [];
  fake.api.scripting.executeScript = () => new Promise(resolve => pending.push(resolve));
  const worker = createChannelBridge(fake.api, fake.dependencies);
  const message = {type: 'channel-status', channelUrl: CHANNEL, videoId: null};
  const first = worker.handle(message, SENDER), second = worker.handle(message, SENDER);
  await new Promise(setImmediate);
  assert.equal(pending.length, 2);
  assert.deepEqual(await worker.handle(message, SENDER), {status: 'unavailable'});
  for (const finish of pending) finish([{frameId: 0, result: {status: 'unknown'}}]);
  await Promise.all([first, second]);
});

test('worker restart retains identity but verifies current tracking instead of replaying an add', async () => {
  const fake = browser();
  const url = 'https://www.youtube.com/@owner';
  await createChannelBridge(fake.api, fake.dependencies).handle({type:'add-channel', channelUrl:url}, SENDER);
  fake.events.length = 0;
  fake.api.scripting.executeScript = async value => {
    fake.events.push(['execute', value]);
    assert.deepEqual(value.args, [ORIGIN, url, lookup(ID)]);
    return [{frameId:0, result:{status:'not_tracked'}}];
  };
  const restarted = createChannelBridge(fake.api, fake.dependencies);
  assert.deepEqual(await restarted.handle({type:'channel-status',channelUrl:url,videoId:null},SENDER),{status:'not_tracked'});
  assert.equal(fake.events.filter(([name]) => name === 'execute').length, 1);
  assert.equal(fake.events.some(([name]) => ['intent','create','update','options'].includes(name)), false);
});

test('identity hints expire, do not cross installations and cannot claim an active session', async () => {
  const fake = browser();
  const url = 'https://www.youtube.com/@owner';
  const message = {type:'channel-status',channelUrl:url,videoId:null};
  fake.session.channelIdentities = [{origin:ORIGIN,url,id:ID,at:1000}];
  let lookupId;
  fake.api.scripting.executeScript = async value => {
    lookupId=JSON.parse(value.args[2]).channelId;
    return [{frameId:0,result:{status:'login_required'}}];
  };
  assert.deepEqual(await createChannelBridge(fake.api,fake.dependencies).handle(message,SENDER),{status:'login_required'});
  assert.equal(lookupId,ID);
  fake.advance(30*60*1000);
  await createChannelBridge(fake.api,fake.dependencies).handle(message,SENDER);
  assert.equal(lookupId,null);
  fake.session.channelIdentities[0].at=1801000;
  fake.local.appOrigin='http://127.0.0.1:8021'; fake.tab.url=fake.local.appOrigin+'/app-explorer-v3';
  await createChannelBridge(fake.api,fake.dependencies).handle(message,SENDER);
  assert.equal(lookupId,null);
});

test('passive identity evidence survives suspension without extending a reused hint lifetime', async () => {
  const fake=browser(), url='https://www.youtube.com/@owner';
  const sender={...SENDER,url:'https://www.youtube.com/watch?v=abcdefghijk'};
  const message={type:'channel-status',channelUrl:url,videoId:'abcdefghijk'};
  const ids=[];
  fake.api.scripting.executeScript=async value=>{ids.push(JSON.parse(value.args[2]).channelId);return [{frameId:0,result:{status:'exists',channelId:ID}}];};
  await createChannelBridge(fake.api,fake.dependencies).handle(message,sender);
  fake.advance(60000);
  await createChannelBridge(fake.api,fake.dependencies).handle(message,sender);
  assert.deepEqual(ids,[null,ID]);
  assert.equal(fake.session.channelIdentities[0].at,1000);
});

test('identity storage remains bounded and storage failure cannot turn an acknowledged add into uncertainty', async () => {
  const fake=browser();
  fake.session.channelIdentities=Array.from({length:100},(_,n)=>({origin:ORIGIN,url:`https://www.youtube.com/@owner${n}`,id:ID,at:1000}));
  await createChannelBridge(fake.api,fake.dependencies).handle(MESSAGE,SENDER);
  assert.equal(fake.session.channelIdentities.length,100);
  assert.equal(fake.session.channelIdentities.some(row=>row.url.endsWith('@owner0')),false);
  fake.api.storage.session.set=async value=>{if ('channelIdentities' in value) throw new Error('storage full');Object.assign(fake.session,value);};
  assert.deepEqual(await createChannelBridge(fake.api,fake.dependencies).handle(MESSAGE,SENDER),success);
});

test('read diagnostics identify browser stages without exposing exception text', async () => {
  const message={type:'channel-status',channelUrl:CHANNEL,videoId:null};
  for(const [stage, mutate] of [
    ['B04', fake=>{fake.api.tabs.query=async()=>{throw new Error('private browser details')};}],
    ['B07', fake=>{fake.api.scripting.executeScript=async()=>{throw new Error('private injection details')};}],
    ['B08', fake=>{fake.api.scripting.executeScript=async()=>[{result:{status:'exists',channelId:ID}}];}],
    ['B93', fake=>{fake.api.scripting.executeScript=async()=>[{frameId:0,result:{status:'unavailable',diagnostic:'secret'}}];}],
  ]) {
    const fake=browser();mutate(fake);
    assert.deepEqual(await createChannelBridge(fake.api,fake.dependencies).handle(message,SENDER),{status:'unavailable',diagnostic:stage});
  }
});

test('failed status reads are not cached across connection recovery', async () => {
  const fake=browser();let calls=0;
  fake.api.scripting.executeScript=async()=>[{frameId:0,result:++calls===1
    ?{status:'unavailable',diagnostic:'A01'}:{status:'exists',channelId:ID}}];
  const worker=createChannelBridge(fake.api,fake.dependencies);
  const message={type:'channel-status',channelUrl:CHANNEL,videoId:null};
  assert.deepEqual(await worker.handle(message,SENDER),{status:'unavailable',diagnostic:'A01'});
  assert.deepEqual(await worker.handle(message,SENDER),{status:'exists',channelId:ID});
  assert.equal(calls,2);
});

test('app read diagnostics distinguish fetching, decoding and contract rejection', async () => {
  for(const [reply,diagnostic] of [
    [()=>{throw new Error('private network details')},'A21'],
    [()=>new Response('<html>private</html>'),'A22'],
    [()=>json({contract_version:99}),'A23'],
  ]) {
    const result=await withPage(async path=>path==='/auth/session'?json(auth()):reply(),
      ()=>addChannelInApp(ORIGIN,'https://www.youtube.com/@owner',lookup(null,'abcdefghijk')));
    assert.deepEqual(result,{status:'unavailable',diagnostic});
  }
});

test('lost read arguments cannot select the write operation', async () => {
  const calls=[];
  await withPage(async (path,init)=>{calls.push([path,init.method]);return json(auth());},async()=>{
    assert.deepEqual(await addChannelInApp(ORIGIN,CHANNEL),{status:'rejected',diagnostic:'A00'});
    assert.deepEqual(calls,[]);
    for(const lookup of [{},false,'read',{channelId:null,videoId:null,extra:true}]) {
      assert.deepEqual(await addChannelInApp(ORIGIN,CHANNEL,lookup),{status:'rejected',diagnostic:'A60'});
    }
  });
  assert.ok(calls.every(([path,method])=>path==='/auth/session'&&method==='GET'));
});

test('unexpected injected results expose a fixed shape code, never their contents', async () => {
  const message={type:'channel-status',channelUrl:CHANNEL,videoId:null};
  for(const [raw,diagnostic] of [
    [{status:'rejected'},'B90'], [{status:'added',channelId:ID},'B91'],
    [{status:'exists',channelId:'invalid'},'B92'], [{status:'unavailable'},'B93'],
    [{private:'not returned'},'B94'], ['private serialized response','B95'],
    [{status:'private unexpected status'},'B96'], [[], 'B97'],
    [{status:'rejected',diagnostic:'A00'},'A00'],
  ]) {
    const fake=browser();
    fake.api.scripting.executeScript=async()=>[{frameId:0,result:raw}];
    assert.deepEqual(await createChannelBridge(fake.api,fake.dependencies).handle(message,SENDER),
      {status:'unavailable',diagnostic});
    assert.ok(!fake.events.some(([kind])=>kind==='create'||kind==='intent'));
  }
});

test('serialized worker and isolated bridge complete cold and warm passive lookup with GET only', async () => {
  const fake=browser(), calls=[];
  fake.api.scripting.executeScript=async ({func,args})=>{
    assert.ok(args.every(value=>value===null||typeof value==='string'));
    const sandbox={location:{origin:ORIGIN},URL,TextDecoder,Uint8Array,AbortController,setTimeout,clearTimeout,
      fetch:async(path,init)=>{
        calls.push([path,init.method]);
        if(path==='/auth/session') return json(auth());
        if(path==='/sidebar-user-data') return json({channels:[{channel_id:ID}]});
        assert.equal(path,'/explorer/channel-library/video-channel?video_id=abcdefghijk');
        return json({contract_version:1,video_id:'abcdefghijk',status:'matched',channel_id:ID});
      },args:JSON.parse(JSON.stringify(args))};
    const result=await vm.runInNewContext(`(${func.toString()})(...args)`,sandbox);
    return [{frameId:0,result:JSON.parse(JSON.stringify(result))}];
  };
  const message={type:'channel-status',channelUrl:'https://www.youtube.com/@owner',videoId:'abcdefghijk'};
  const sender={...SENDER,url:'https://www.youtube.com/watch?v=abcdefghijk'};
  for(let run=0;run<2;run++) {
    assert.deepEqual(await createChannelBridge(fake.api,fake.dependencies).handle(message,sender),{status:'exists',channelId:ID});
  }
  assert.deepEqual(calls,[['/auth/session','GET'],['/explorer/channel-library/video-channel?video_id=abcdefghijk','GET'],
    ['/auth/session','GET'],['/sidebar-user-data','GET']]);
  assert.ok(!fake.events.some(([kind])=>['create','intent','update'].includes(kind)));
});

test('encoded passive requests reject malformed JSON and fields without selecting a write', async () => {
  const calls=[];
  await withPage(async(path,init)=>{calls.push([path,init.method]);return json(auth());},async()=>{
    for(const [encoded,diagnostic] of [
      ['{','A60'], [' '.repeat(257),'A60'], ['null','A61'], ['[]','A61'],
      ['{}','A61'], ['{"channelId":null,"videoId":null,"extra":true}','A61'],
      ['{"channelId":"bad","videoId":null}','A62'],
      ['{"channelId":null,"videoId":12345678901}','A63'],
    ]) assert.deepEqual(await addChannelInApp(ORIGIN,CHANNEL,encoded),{status:'rejected',diagnostic});
  });
  assert.ok(calls.every(([path,method])=>path==='/auth/session'&&method==='GET'));
});
